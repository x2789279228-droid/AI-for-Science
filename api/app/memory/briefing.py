"""研究简报（模块七 R4，清单 §三#7 / §2.2 / §2.12）：假设 vs 其时间戳之后的新证据。

这是 Temporal KG 的 **extrapolation**（用已有证据推断假设「当前」为真状态）——生产
失败高发区，也是「第 30 次会话说出假设被推翻」演示的承载机制。
判定双路（与 extract 同哲学）：
- judge 路：briefing.yaml（版本化）+ json_mode，verdict_source="judge"；
- evidence/rule 路：LLM 离线/失败时按**图谱边**判（refutes→refuted / supports→supported，
  verdict_source="evidence"），有相关新证据但无边→neutral（source="rule"），
  无证据→unverified 不冒充（source=""）。
判定合法时同步推进假设 FSM（proposed→active→supported/refuted；非法转移跳过不抛，
清单 §2.2）。开场下发走 worker 缓存（P1）；R4 先提供按需计算入口。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta, timezone

from ..db import query as db_query
from ..llm import client as llm
from ..llm import prompts
from . import drift, store

log = logging.getLogger("meridian.memory")

_TZ8 = timezone(timedelta(hours=8))

# 内置兜底 prompt（yaml 缺失时 resolve 回退，version='builtin'）
_BUILTIN_BRIEF = (
    "判定研究假设当前状态。输出 JSON："
    '{"verdict":"supported|refuted|neutral","reasoning":"≤80字","confidence":0.0-1.0}。'
    "证据不足就是 neutral，绝不虚构。")

_SIM_THRESHOLD = 0.15          # 词面相关阈值（bigram 包含度）——新证据收集用
ACTIVE_STATUSES = ("proposed", "active", "supported")


def _iso(ts: float) -> str:
    """UTC epoch → ISO 8601（+08:00 偏移，清单 §2.4）。"""
    return datetime.fromtimestamp(ts, tz=_TZ8).isoformat()


def _bigrams(text: str) -> set[str]:
    from .retrieve import _bigrams as _b
    return _b(text)


def _sim_to_hyp(hyp: dict, text: str) -> float:
    q = _bigrams(f"{hyp['content']} {hyp.get('subject') or ''}")
    if not q:
        return 0.0
    return len(q & _bigrams(text)) / len(q)


def collect_new_evidence(hyp: dict, *, user_id: str = "local") -> list[dict]:
    """hyp.ts 之后出现的新证据（三类，全部带溯源字段）：
    1) 记忆事件 experiment_result/finding（词面相关 ≥ 阈值）；
    2) 指向该假设的 active 图谱边（relation 本身即判定信号）；
    3) tracked_items 新条目（标题/摘要词面相关）。"""
    out: list[dict] = []
    seen_ids: set[str] = set()
    for ev in store.list_events(limit=300):
        if ev["user_id"] != user_id or ev["id"] == hyp["id"] or ev["ts"] <= hyp["ts"]:
            continue
        if ev["kind"] not in ("experiment_result", "finding", "decision"):
            continue
        sim = _sim_to_hyp(hyp, f"{ev['content']} {ev.get('subject') or ''}")
        if sim < _SIM_THRESHOLD:
            continue
        seen_ids.add(ev["id"])
        out.append({"source": "memory", "kind": ev["kind"], "content": ev["content"],
                    "memory_id": ev["id"], "conversation_id": ev["conversation_id"],
                    "turn": ev["turn"], "thread_id": ev["thread_id"],
                    "e_refs": (ev.get("meta") or {}).get("e_refs") or [],
                    "ts": ev["ts"], "ts_iso": _iso(ev["ts"]), "sim": round(sim, 4)})
    for edge in store.list_edges(dst=hyp["id"], status="active"):
        if edge["relation"] not in ("refutes", "supports", "supersedes"):
            continue
        if edge["src"] in seen_ids:
            for item in out:
                if item["memory_id"] == edge["src"]:
                    item["relation"] = edge["relation"]
            continue
        out.append({"source": "edge", "kind": edge["relation"], "content":
                    f"（图谱边：{edge['relation']}，confirm_count={edge['confirm_count']}）",
                    "memory_id": edge["src"], "conversation_id": "", "turn": 0,
                    "thread_id": "", "e_refs": [], "ts": edge["ts"],
                    "ts_iso": _iso(edge["ts"]), "relation": edge["relation"]})
    for row in db_query("SELECT id, title, abstract, added_at FROM tracked_items "
                        "WHERE added_at>? ORDER BY added_at DESC LIMIT 50", (hyp["ts"],)):
        text = f"{row['title']} {row['abstract'] or ''}"
        if _sim_to_hyp(hyp, text) < _SIM_THRESHOLD:
            continue
        out.append({"source": "tracked", "kind": "tracked_item", "content": row["title"][:120],
                    "memory_id": "", "conversation_id": "", "turn": 0, "thread_id": "",
                    "e_refs": [], "ts": row["added_at"], "ts_iso": _iso(row["added_at"])})
    return out


def _judge_rule(hyp: dict, evidence: list[dict]) -> tuple[str, str, str]:
    """规则判定：图谱边优先（evidence 源），其次中性（rule 源），无证据 unverified。"""
    relations = {e.get("relation") for e in evidence if e.get("relation")}
    if "refutes" in relations or "supersedes" in relations:
        return "refuted", "evidence", "新证据与假设构成反驳/取代边"
    if "supports" in relations:
        return "supported", "evidence", "新证据与假设构成支持边"
    if evidence:
        return "neutral", "rule", "存在相关新证据但未构成判定边，待复核"
    return "unverified", "", "假设提出后暂无相关新证据"


def _judge_llm(hyp: dict, evidence: list[dict]) -> tuple[str, str, str, float] | None:
    """judge 路；离线/失败/非法 verdict 返回 None（调用方转规则路）。"""
    if not evidence or not llm.available():
        return None
    text, version = prompts.resolve("briefing", "system_brief", _BUILTIN_BRIEF)
    ev_txt = "\n".join(f"- [{e['source']}/{e['kind']}] {e['content'][:150]}" for e in evidence[:6])
    try:
        res = llm.chat(
            [{"role": "system", "content": text},
             {"role": "user", "content":
              f"<hypothesis>{hyp['content']}</hypothesis>\n"
              f"<evidence>\n{ev_txt}\n</evidence>"}],
            json_mode=True, max_tokens=300, module="briefing",
            audit_prompt=hyp["content"][:120], prompt_version=version)
    except Exception as e:  # noqa: BLE001 —— judge 失败转规则路
        log.warning("briefing judge 失败 → 规则路: %s", e)
        return None
    if not res.ok:                       # LLMResult 是 dict：.ok = bool(text)
        return None
    try:
        m = re.search(r"\{.*\}", res.get("text") or "", re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except (ValueError, TypeError):
        return None
    verdict = data.get("verdict")
    if verdict not in ("supported", "refuted", "neutral"):
        return None
    conf = data.get("confidence")
    return verdict, "judge", str(data.get("reasoning") or "")[:160], \
        float(conf) if isinstance(conf, (int, float)) else 0.5


def _apply_verdict_transition(hyp: dict, verdict: str) -> list[str]:
    """判定为终态时推进 FSM（合法转移表强制；非法/已终态跳过——如实记 applied）。"""
    applied: list[str] = []
    if verdict not in ("supported", "refuted"):
        return applied
    try:
        if hyp["status"] == "proposed":
            store.transition(hyp["id"], "active",
                             why="出现相关新证据（简报复核触发）", verdict_source="evidence")
            applied.append("proposed→active")
        cur = store.get_event(hyp["id"]) or {}
        store.transition(hyp["id"], verdict,
                         why=f"简报复核判定 {verdict}", verdict_source="evidence")
        applied.append(f"{cur.get('status')}→{verdict}")
    except store.FSMError as e:
        log.info("简报 FSM 转移跳过（非法/已终态）: %s", e)
    return applied


def build_brief(*, conversation_id: str = "", user_id: str = "local",
                now_ts: float | None = None, use_judge: bool = True) -> dict:
    """构建开场研究简报（extrapolation）。返回 items[]：每条 active/proposed/supported
    假设的当前判定 + 证据链（附 M 回放字段与 E# 引用），无证据标 unverified 不冒充。"""
    hyps = [h for h in store.list_events(kind="hypothesis", limit=100)
            if h["user_id"] == user_id and h["status"] in ACTIVE_STATUSES
            and (not conversation_id or h["conversation_id"] == conversation_id)]
    items: list[dict] = []
    engine = "rule"
    for hyp in hyps:
        evidence = collect_new_evidence(hyp, user_id=user_id)
        judged = _judge_llm(hyp, evidence) if use_judge and llm.available() else None
        if judged:
            verdict, v_source, reasoning, confidence = judged
            engine = "judge"
        else:
            verdict, v_source, reasoning = _judge_rule(hyp, evidence)
            confidence = 1.0 if v_source == "evidence" else (0.3 if verdict == "neutral" else 0.0)
        transitions = _apply_verdict_transition(hyp, verdict) if v_source in ("evidence", "judge") \
            else []
        cur = store.get_event(hyp["id"]) or hyp
        items.append({
            "hypothesis": {"id": hyp["id"], "content": hyp["content"],
                           "subject": hyp.get("subject") or "",
                           "status": cur["status"], "ts": hyp["ts"], "ts_iso": _iso(hyp["ts"]),
                           "conversation_id": hyp["conversation_id"], "turn": hyp["turn"],
                           "thread_id": hyp["thread_id"]},
            "verdict": verdict, "verdict_source": v_source,
            "reasoning": reasoning, "confidence": round(confidence, 2),
            "evidence": evidence, "transitions_applied": transitions,
        })
    # 终态假设摘要（被推翻/被取代的历史——信念演化史，THEANINE 前提）
    past = [h for h in store.list_events(kind="hypothesis", limit=100)
            if h["user_id"] == user_id and h["status"] in ("refuted", "superseded")]
    return {"generated_at": _iso(now_ts) if now_ts is not None else _iso(
                datetime.now(UTC).timestamp()),
            "engine": engine,
            "items": items,
            "past_hypotheses": [{"id": h["id"], "content": h["content"], "status": h["status"],
                                 "ts_iso": _iso(h["ts"])} for h in past[:10]],
            # ATLAS 漂移降级口径（r8）：文字提示，借思想非隐层干预（清单 §七.5）
            "drift_hint": drift.drift_hint(user_id=user_id),
            "meta": {"n_hypotheses": len(hyps), "n_items": len(items)}}


# ---------- 开场简报缓存（清单 §2.7：chat 开场零 LLM 阻塞直发） ----------
# key = user::conversation::memory_events 代数戳——事件增删自动失效（免 briefing↔store
# 循环依赖）；未命中时同步走规则路（毫秒级、零 LLM），LLM 可用则后台线程以 judge 重算
# 回填（最终一致）。P1 worker 接管预热后，此线程退化为兜底。
_BRIEF_CACHE: dict[str, tuple[float, dict]] = {}
_BRIEF_TTL_S = 300.0


def _cache_generation() -> int:
    rows = db_query("SELECT COUNT(*) AS c FROM memory_events")
    return int(rows[0]["c"]) if rows else 0


def cached_brief(*, conversation_id: str = "", user_id: str = "local") -> dict:
    """chat 开场直发用：TTL + 代数戳缓存；空简报返回 {"empty": True}（前端不渲染卡片）。"""
    key = f"{user_id}::{conversation_id}::{_cache_generation()}"
    now = time.time()
    hit = _BRIEF_CACHE.get(key)
    if hit and now - hit[0] < _BRIEF_TTL_S:
        return hit[1]
    brief = build_brief(conversation_id=conversation_id, user_id=user_id, use_judge=False)
    if not brief["items"] and not brief["past_hypotheses"]:
        brief = {"empty": True, "generated_at": _iso(now)}
    _BRIEF_CACHE[key] = (now, brief)
    if llm.available():
        threading.Thread(target=_refresh_with_judge, args=(key, conversation_id, user_id),
                         daemon=True, name="brief-judge-refresh").start()
    return brief


def _refresh_with_judge(key: str, conversation_id: str, user_id: str) -> None:
    """judge 重算回填（后台，不阻塞开场；失败静默保留规则版）。"""
    try:
        brief = build_brief(conversation_id=conversation_id, user_id=user_id)
        if not brief["items"] and not brief["past_hypotheses"]:
            brief = {"empty": True, "generated_at": _iso(time.time())}
        _BRIEF_CACHE[key] = (time.time(), brief)
    except Exception as e:  # noqa: BLE001 —— 缓存刷新失败不影响已下发内容
        log.warning("简报 judge 回填失败: %s", type(e).__name__)
