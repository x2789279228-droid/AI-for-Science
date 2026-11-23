"""记忆问答（模块七 r9，A/B 评测三轮实测驱动的架构级分流）。

问题：记忆注入（user 块 + system 例外条款）仍不足以让文献问答角色正确回答
「我之前的假设现在怎么样」——模型把问题框进论文证据环，产出分级拒答（三轮实测，
详见 acceptance/round9-20261121-r9）。正确解：**记忆型问题由记忆时间线直接作答**
（独立 prompt + [M#] 引用），不走论文证据环——与项目「router 意图分流」哲学一致。

双路：LLM 路（memory_refine.yaml，仅依据时间线回答）+ 离线确定性兜底（时间线原文
罗列 + FSM 状态如实标注）。两条路都只引用时间线内信息，禁止编造。
"""
from __future__ import annotations

import logging
import re

from ..llm import client as llm
from ..llm import prompts

log = logging.getLogger("meridian.memory")

_BUILTIN_MEMORY_QA = (
    "你是用户的科研记忆助手。只依据记忆时间线回答用户关于其历史研究的问题，"
    "每个论断紧跟 [M编号] 引用；refuted/superseded 的记忆必须说明其已不成立；"
    "时间线没有的信息如实说明，禁止编造。中文回答，≤250 字。")

_STATUS_LABEL = {"proposed": "提出", "active": "验证中", "supported": "已支持",
                 "refuted": "已推翻", "superseded": "已被取代"}

_MEMORY_QA_TOP_REL = 0.1     # 记忆问答分流门槛（r9 第四轮实测校准：自然提问与记忆的
                             # 词面重叠常仅 0.1~0.3，0.2 门槛会让分流永不触发）


def _timeline_lines(anchors: dict[str, dict]) -> list[str]:
    """带 FSM 状态的时间线文本（hypothesis 标注当前状态——判定语义的关键）。"""
    lines = []
    for mid, a in anchors.items():
        status = ""
        if a.get("kind") == "hypothesis" and a.get("status"):
            status = f"（当前状态：{_STATUS_LABEL.get(a['status'], a['status'])}）"
        lines.append(f"[{mid}] {a.get('content', '')}{status}")
    return lines


def answer_from_memory(question: str, anchors: dict[str, dict], *,
                       use_llm: bool = True) -> dict:
    """由记忆时间线直接回答（LLM 路 / 离线确定性兜底）。

    返回 {"answer": str, "mode": "llm"|"extractive", "m_citations": int}。
    两条路都只使用时间线内的信息；LLM 不可用/失败/输出无 [M#] 引用时走兜底。"""
    lines = _timeline_lines(anchors)
    if not lines:
        return {"answer": "记忆时间线为空，暂无可回答的历史研究记录。", "mode": "extractive",
                "m_citations": 0}
    timeline = "\n".join(lines)

    if use_llm and llm.available():
        text, version = prompts.resolve("memory_refine", "system_memory_qa",
                                        _BUILTIN_MEMORY_QA)
        try:
            # 时间线放 user 消息且问题置于其后（r9 第六轮实测：放 system 时 lite 模型
            # 会无视时间线内容回答「没有相关记录」——user 内容注意力更强）
            res = llm.chat(
                [{"role": "system", "content": text},
                 {"role": "user", "content":
                  f"记忆时间线（[M#] 编号，hypothesis 附当前状态）：\n{timeline}\n\n"
                  f"用户问题：{question}"}],
                max_tokens=500, module="memory_refine",
                audit_prompt=question[:100], prompt_version=version)
            if res.ok:
                answer = (res.get("text") or "").strip()
                if answer and re.search(r"\[M\d+\]", answer):
                    return {"answer": answer, "mode": "llm",
                            "m_citations": len(re.findall(r"\[M\d+\]", answer))}
                log.warning("memory_qa LLM 输出缺 [M#] 引用 → 离线兜底")
        except Exception as e:  # noqa: BLE001 —— LLM 失败走确定性兜底
            log.warning("memory_qa LLM 失败 → 离线兜底: %s", type(e).__name__)
    else:
        # 触达一次 prompts.resolve 保持审计口径一致（版本写入 qa_logs 由调用方决定）
        _ = prompts.resolve("memory_refine", "system_memory_qa", _BUILTIN_MEMORY_QA)

    # 离线确定性兜底：时间线原文罗列 + hypothesis 状态如实标注（不编造、逐条可溯源）
    answer = ("以下是你记忆时间线中的相关记录（离线口径，逐条可溯源）：\n"
              + "\n".join(f"- {ln}" for ln in lines)
              + "\n\n（LLM 暂不可用，以上为时间线原文；状态以 FSM 当前状态为准。）")
    return {"answer": answer, "mode": "extractive",
            "m_citations": len(re.findall(r"\[M\d+\]", answer))}


def gate_from_ranked(items: list[dict]) -> dict:
    """记忆问答分流门：最强候选的原始相关度 ≥ 门槛 → 触发（responder 消费）。"""
    top = max((i["scores"].get("relevance_raw", 0.0) for i in items
               if i["scores"].get("hops", 0) == 0), default=0.0)
    return {"trigger": top >= _MEMORY_QA_TOP_REL, "top_relevance_raw": round(top, 4),
            "threshold": _MEMORY_QA_TOP_REL}
