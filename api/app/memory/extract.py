"""轮末记忆抽取（模块七 R2，清单 §三#4 / §2.8）：Q&A → 记忆事件，问答主链路延迟零增加。

双路设计：
- LLM 路：memory_extract.yaml（版本化）+ json_mode 结构化抽取，source=llm；
- 规则路：LLM 离线/失败/解析失败时句式规则兜底（"我假设/实验结果表明/我决定…"），
  source=rule 如实标注（与 DocIR 的诚实降级同口径）。
抽取总入口 extract_turn() 永不抛异常——runner 的 daemon 钩子在 done 之后跑，
失败只落 qa_logs(guardrail=memory_extract_error) 审计，绝不影响问答闭环。
"""
from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar

from ..db import log_qa
from ..llm import client as llm
from ..llm import prompts
from . import store

log = logging.getLogger("meridian.memory")

# 当前问答轮次上下文（runner 线程置值；memory_save 工具读取以落会话/轮次溯源）
# 形如 (conversation_id, turn, thread_id)
turn_ctx: ContextVar[tuple[str, int, str]] = ContextVar("memory_turn_ctx",
                                                        default=("", 0, ""))

_MAX_EVENTS_PER_TURN = 6

# 内置兜底 prompt（yaml 缺失/非法时 resolve 回退，version='builtin'）
_BUILTIN_EXTRACT = (
    "从本轮提问与回答中抽取值得跨会话记住的事实，输出 JSON："
    '{"events":[{"kind":"hypothesis|experiment_result|finding|decision|preference|topic",'
    '"content":"...","subject":"...","entities":["..."],"importance":1-10}]}。'
    "只抽事实与决定，无则输出 {\"events\":[]}。")

# 规则兜底句式（踩坑 #3：规则版是降级路径不是主路径，source 如实标 rule）
_RULES: list[tuple[str, re.Pattern]] = [
    ("hypothesis", re.compile(r"(我(们)?(假设|猜想|推测|预计)|我的假设是|hypothes[ise]z)", re.IGNORECASE)),
    ("preference", re.compile(r"(我(比较)?(喜欢|偏好|倾向)|请(一直)?用|以后都用|回答用(中文|英文))", re.IGNORECASE)),
    ("decision", re.compile(r"(我(们)?决定|决定采用|决定改用|选择用|就用)")),
    ("experiment_result", re.compile(r"(实验(结果|表明|显示)|结果表明|数据显示|评测(结果|显示)|benchmark)", re.IGNORECASE)),
    ("finding", re.compile(r"(我们发现|可以得出|结论是|研究表明|说明)")),
]
_RULE_IMPORTANCE = {"hypothesis": 7, "decision": 7, "preference": 8,
                    "experiment_result": 6, "finding": 6, "topic": 4}
_SENT_SPLIT = re.compile(r"[。！？!?\n；;]+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if 4 <= len(s.strip()) <= 160]


def _rule_extract(question: str, answer: str) -> list[dict]:
    """句式规则兜底：命中句式 → 候选事件（importance 走先验表）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for sent in _sentences(f"{answer}") + _sentences(question):
        for kind, pat in _RULES:
            m = pat.search(sent)
            if not m:
                continue
            content = sent[:120]
            if content in seen:
                break
            seen.add(content)
            out.append({"kind": kind, "content": content,
                        "subject": "", "entities": [],
                        "importance": _RULE_IMPORTANCE[kind]})
            break
        if len(out) >= _MAX_EVENTS_PER_TURN:
            break
    return out


def _parse_llm_payload(text: str) -> list[dict]:
    """宽容解析 LLM JSON（剥围栏/取首个大括号块）；字段非法的条目丢弃。"""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return []
    data = json.loads(m.group(0))
    events = data.get("events")
    if not isinstance(events, list):
        return []
    out = []
    for e in events[:_MAX_EVENTS_PER_TURN]:
        if not isinstance(e, dict) or not isinstance(e.get("content"), str) \
                or not e["content"].strip() or e.get("kind") not in store.EVENT_KINDS:
            continue
        imp = e.get("importance")
        out.append({"kind": e["kind"], "content": e["content"].strip()[:120],
                    "subject": str(e.get("subject") or "")[:60],
                    "entities": [str(x) for x in (e.get("entities") or [])[:5]],
                    "importance": int(imp) if isinstance(imp, (int, float))
                    and 1 <= imp <= 10 else 5})
    return out


def _llm_extract(question: str, answer: str) -> list[dict] | None:
    """LLM 路；不可用/失败/空结果返回 None（调用方转规则路）。"""
    if not llm.available():
        return None
    text, version = prompts.resolve("memory_extract", "system_extract", _BUILTIN_EXTRACT)
    try:
        res = llm.chat(
            [{"role": "system", "content": text},
             {"role": "user", "content":
              f"<question>{question[:500]}</question>\n<answer>{answer[:4000]}</answer>"}],
            json_mode=True, max_tokens=800, module="memory_extract",
            audit_prompt=question[:200], prompt_version=version)
    except Exception as e:  # noqa: BLE001 —— LLM 路任何异常都转规则路
        log.warning("memory extract llm 失败 → 规则兜底: %s", e)
        return None
    if not res.ok:                       # LLMResult 是 dict：.ok = bool(text)
        return None
    try:
        return _parse_llm_payload(res.get("text") or "")
    except (ValueError, TypeError):
        return None


def extract_turn(question: str, answer: str, *, conversation_id: str = "", turn: int = 0,
                 thread_id: str = "", user_id: str = "local",
                 e_refs: list[str] | None = None) -> dict:
    """抽取总入口（永不抛异常）。返回 {"created": [...], "engine": llm|rule, "error": ""}。

    e_refs：本轮 Evidence Pack 的 [E#] 编号列表（清单 §2.8）——记入每条事件 meta，
    构成 M#↔E# 同源双链。"""
    created: list[dict] = []
    engine, error = "rule", ""
    try:
        cands = _llm_extract(question, answer)
        if cands is not None:
            engine = "llm"
        else:
            cands = _rule_extract(question, answer)
        seen: set[str] = set()
        # 与 memory_save（agent 主动写入）去重：同会话同轮已有同内容事件 → 抽取跳过
        existing = {e["content"] for e in store.list_events(conversation_id=conversation_id,
                                                            limit=50) if e["turn"] == turn}
        for c in cands:
            if c["content"] in seen or c["content"] in existing:
                continue
            seen.add(c["content"])
            ev = store.add_event(
                c["kind"], c["content"], conversation_id=conversation_id, turn=turn,
                thread_id=thread_id, user_id=user_id, subject=c["subject"],
                entities=c["entities"], importance=c["importance"],
                source="llm" if engine == "llm" else "rule",
                meta={"e_refs": e_refs or []})
            created.append(ev)
    except Exception as e:  # noqa: BLE001 —— 记忆抽取失败绝不影响问答闭环
        error = f"{type(e).__name__}: {e}"
        log.warning("memory extract 失败（已审计）: %s", error)
        try:
            log_qa(module="memory", question=question[:200],
                   guardrail=f"memory_extract_error:{error[:120]}")
        except Exception:  # noqa: BLE001, S110 —— 审计失败也不抛
            pass
    return {"created": created, "engine": engine, "error": error}
