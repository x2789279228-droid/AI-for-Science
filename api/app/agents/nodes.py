"""LangGraph 状态图节点。

主图节点：router / planner / verifier / responder（同步函数，LLM 复用 app/llm/client.py）。
Executor 子图节点：executor_entry(透传) → retrieve_task(Send 并行扇出) → executor_fuse(RRF)。
可视化：所有节点完成时经 config.configurable.queue 推送「节点即卡片」trace 事件；
state.trace 同步累积（最终随 final 事件整体下发，前端以其为准）。
"""
from __future__ import annotations

import queue as queue_mod
from typing import Any

from langgraph.types import Send

from ..llm import client as llm
from ..rag import indexing
from ..rag.answering import (
    NO_EVIDENCE, REFUSAL_CHITCHAT, build_qa_messages, extractive_answer,
    format_context, llm_claims, validate_citations,
)
from .state import AgentState, Step, trace_card

_GREETINGS = {"你好", "您好", "hi", "hello", "在吗", "你是谁", "测试", "hello world", "ping"}
_SUMMARY_HINTS = ("总结", "综述", "概括", "summary", "summarize", "主要讲", "讲了什么")


def _queue(config: dict | None) -> queue_mod.Queue | None:
    return ((config or {}).get("configurable") or {}).get("queue")


def _emit(config: dict | None, card: dict) -> None:
    q = _queue(config)
    if q is not None:
        q.put(("trace", card))


# ---------------- Router ----------------

def _rule_intent(question: str, paper_ids: list[str]) -> dict:
    q = question.strip().lower()
    if q in _GREETINGS or len(q) < 3:
        return {"type": "chitchat", "aspects": [], "language": "zh"}
    if any(h in question for h in _SUMMARY_HINTS):
        return {"type": "summarize", "aspects": [question], "language": "zh"}
    if len(paper_ids) > 1:
        return {"type": "qa_compare", "aspects": [question], "language": "zh"}
    return {"type": "qa_single", "aspects": [question], "language": "zh"}


def router_node(state: AgentState, config=None) -> dict:
    question = state["question"]
    paper_ids = state["paper_ids"]
    intent: dict | None = None
    if llm.available():
        res = llm.chat(
            [{"role": "system", "content":
                "你是意图路由器。把用户问题分类为以下之一：qa_single(单篇细节问答)、qa_compare(多论文对比)、"
                "summarize(总结/综述)、graph_query(引用关系)、writing(写作请求)、chitchat(寒暄/无关)。"
                '输出 JSON：{"type":"...","aspects":["面向检索的子问题1","子问题2"]}。'
                "aspects 为 2~4 个可独立检索的子问题（chitchat 时为空数组），使用与问题相同的语言。"},
             {"role": "user", "content": f"库内论文数：{len(paper_ids)}\n问题：{question}"}],
            json_mode=True, temperature=0.0, module="router", audit_prompt=question[:200],
        )
        data = llm.parse_json(res.get("text", ""))
        if data and data.get("type") in {"qa_single", "qa_compare", "summarize",
                                         "graph_query", "writing", "chitchat"}:
            aspects = [a for a in (data.get("aspects") or []) if isinstance(a, str) and a.strip()]
            if data["type"] != "chitchat" and not aspects:
                aspects = [question]
            intent = {"type": data["type"], "aspects": aspects, "language": "zh"}
        else:
            intent = None  # 解析失败 → 规则兜底
    if intent is None:
        intent = _rule_intent(question, paper_ids)

    card = trace_card("router", "意图识别",
                      f"{intent['type']} · {len(intent['aspects'])} 个检索面",
                      {"intent": intent})
    _emit(config, card)
    return {"intent": intent, "trace": [card]}


# ---------------- Planner ----------------

def _rule_steps(question: str, paper_ids: list[str], aspects: list[str]) -> list[Step]:
    eff_aspects = [a.strip() for a in aspects if a and a.strip()] or [question]
    steps: list[Step] = []
    for aspect in eff_aspects:
        for pid in paper_ids:
            steps.append({"paper_id": pid, "query": aspect, "aspect": aspect})
    return steps


def planner_node(state: AgentState, config=None) -> dict:
    question = state["question"]
    paper_ids = state["paper_ids"]
    intent = state.get("intent", {})
    aspects = intent.get("aspects") or [question]
    retries = state.get("retries", 0)

    # 默认规则计划
    steps = _rule_steps(question, paper_ids, aspects)
    mode = "compare" if intent.get("type") == "qa_compare" else \
           "summary" if intent.get("type") == "summarize" else "single"
    guardrails: list[str] = []

    # LLM 可用时：把 aspects 细化为更可检索的子查询（重规划时换温度求变体）
    if llm.available() and len(paper_ids) > 0:
        temp = 0.3 if retries > 0 else 0.0
        res = llm.chat(
            [{"role": "system", "content":
                "你是检索规划器。把每个检索面改写为更适合学术 PDF 全文检索的英文子查询"
                "（保留原意、补全术语，如 GPU/训练 → training hardware GPUs）。"
                '输出 JSON：{"queries":["..."]}，数量与输入检索面一一对应。'},
             {"role": "user", "content": "检索面：\n" + "\n".join(f"- {a}" for a in aspects)}],
            json_mode=True, temperature=temp, module="planner", audit_prompt=question[:200],
        )
        data = llm.parse_json(res.get("text", ""))
        queries = data.get("queries") if isinstance(data, dict) else None
        if isinstance(queries, list) and len(queries) == len(aspects):
            steps = [{"paper_id": pid, "query": str(q).strip() or aspect, "aspect": aspect}
                     for aspect, q in zip(aspects, queries) for pid in paper_ids]
        else:
            guardrails.append("planner_llm_fallback")

    plan = {"steps": steps, "mode": mode}
    title = "任务规划" + ("（重规划）" if retries > 0 else "")
    card = trace_card("planner", title, f"{len(steps)} 步 · {len(paper_ids)} 篇 × {len(aspects)} 面",
                      {"steps": steps})
    _emit(config, card)
    return {"plan": plan, "trace": [card], "guardrails": guardrails}


# ---------------- Executor 子图 ----------------

def executor_entry_node(state: AgentState) -> dict:
    return {}  # 透传；扇出在出边完成


def fan_out(state: AgentState):
    """显式 Send 并行扇出：每条 step 一路检索。空计划直接进 fuse。"""
    steps = state.get("plan", {}).get("steps", [])
    if not steps:
        return "executor_fuse"
    return [Send("retrieve_task", {"step": s, "retries": state.get("retries", 0),
                                   "question": state.get("question", "")})
            for s in steps]


def retrieve_task_node(payload: dict, config=None) -> dict:
    step: Step = payload["step"]
    retries = payload.get("retries", 0)
    top_k = 10 + retries * 6  # 重规划时放宽召回
    search = indexing.hybrid_search([step["paper_id"]], step["query"], top_k=top_k)
    tagged = [{**h, "aspect": step["aspect"], "query": step["query"]} for h in search["hits"]]
    stat = {"paper_id": step["paper_id"], "query": step["query"], "aspect": step["aspect"],
            "hits": len(tagged), "channels": search["channels"],
            "bm25": search["bm25_count"], "dense": search["dense_count"]}
    card = trace_card("retrieve_task", "并行检索",
                      f"{step['query'][:26]}… → {len(tagged)} 块" if len(step["query"]) > 26
                      else f"{step['query']} → {len(tagged)} 块",
                      stat)
    _emit(config, card)
    return {"raw_hits": tagged, "branch_stats": [stat], "trace": [card]}


def fuse_node(state: AgentState, config=None) -> dict:
    results = state.get("raw_hits", [])
    if results:
        # 每个 (aspect, paper) 分支是一路检索，跨分支 RRF 融合去重
        groups: dict[tuple[str, str], list[dict]] = {}
        for h in results:
            groups.setdefault((h.get("aspect", ""), h.get("paper_id", "")), []).append(h)
        fused = indexing.rrf_fuse(*[(f"b{i}", lst) for i, lst in enumerate(groups.values())],
                                  top_k=8)
    else:
        fused = []
    channels = sorted({c for b in state.get("branch_stats", []) for c in b.get("channels", [])})
    card = trace_card("executor_fuse", "证据融合",
                      f"RRF · {len(results)} 路原始 → {len(fused)} 块证据 · 通道 {'+'.join(channels) or 'bm25'}",
                      {"channels": channels, "branch_count": len(state.get("branch_stats", []))})
    _emit(config, card)
    return {"evidence": fused, "trace": [card]}


# ---------------- Verifier ----------------

def _check_sufficiency(state: AgentState) -> bool:
    """证据充分性：每个检索面至少 1 条有效命中。"""
    plan = state.get("plan", {})
    evidence = state.get("evidence", [])
    if not evidence:
        return False
    aspects = {s.get("aspect", "") for s in plan.get("steps", [])}
    if not aspects:
        return True
    hit_aspects = {h.get("aspect", "") for h in evidence}
    return aspects <= hit_aspects


def verifier_node(state: AgentState, config=None) -> dict:
    ok = _check_sufficiency(state)
    if ok:
        card = trace_card("verifier", "证据校验", f"通过 · {len(state.get('evidence', []))} 块覆盖全部检索面",
                          {"sufficient": True})
        _emit(config, card)
        return {"trace": [card]}
    missing = {s.get("aspect", "") for s in state.get("plan", {}).get("steps", [])} - \
              {h.get("aspect", "") for h in state.get("evidence", [])}
    card = trace_card("verifier", "证据校验", f"不足 · 缺少 {len(missing)} 个检索面，触发重规划",
                      {"sufficient": False, "missing": sorted(missing)})
    _emit(config, card)
    return {"retries": state.get("retries", 0) + 1,
            "guardrails": [f"insufficient_evidence:{int(state.get('retries', 0)) + 1}"],
            "trace": [card]}


# ---------------- Responder ----------------

def responder_node(state: AgentState, config=None) -> dict:
    q = _queue(config)
    question = state["question"]
    intent_type = state.get("intent", {}).get("type", "qa_single")
    guardrails: list[str] = []

    def finish(answer: str, claims: list[dict], mode: str, summary: str) -> dict:
        card = trace_card("responder", "生成回答", summary,
                          {"mode": mode, "claims": len(claims)})
        _emit(config, card)
        return {"answer": answer, "claims": claims, "mode": mode,
                "guardrails": guardrails, "trace": [card]}

    if intent_type == "chitchat":
        return finish(REFUSAL_CHITCHAT, [], "refusal", "寒暄拒答（范围外）")

    hits = state.get("evidence", [])
    if not hits:
        return finish(NO_EVIDENCE, [], "none", "无证据 → 明确拒答")

    page_map = format_context(hits)[1]

    if not llm.available():
        answer, claims = extractive_answer(question, hits)
        guardrails.append("llm_unavailable_fallback")
        return finish(answer, claims, "extractive",
                      f"抽取式降级 · {len(claims)} 条论断")

    # 生成式：流式输出 → 引用后验 → ClaimTrace
    messages = build_qa_messages(question, hits)
    parts: list[str] = []
    streamed = False
    for piece in llm.stream_chat(messages, max_tokens=1500):
        streamed = True
        parts.append(piece)
        if q is not None:
            q.put(("delta", {"text": piece}))
    answer = "".join(parts)
    if not streamed or not answer.strip():
        res = llm.chat(messages, max_tokens=1500, module="qa", audit_prompt=question)
        answer = res.get("text", "")
        if answer and q is not None:
            q.put(("delta", {"text": answer}))

    if not answer.strip():
        guardrails.append("llm_unavailable_fallback")
        answer, claims = extractive_answer(question, hits)
        return finish(answer, claims, "extractive", "流式失败 → 抽取式降级")

    answer, cited_guards = validate_citations(answer, page_map)
    guardrails += cited_guards
    if "no_citation_found" in guardrails:
        retry = llm.chat(messages + [
            {"role": "assistant", "content": answer},
            {"role": "user", "content":
                "你的回答缺少引用标记。请严格按 [^p{页码}-§{章节}] 格式重写，只改引用不改内容。"}],
            max_tokens=1500, module="qa_retry")
        if retry.get("text"):
            answer, g2 = validate_citations(retry["text"], page_map)
            guardrails += g2
    if "no_citation_found" in guardrails:
        answer += "\n\n> ⚠️ 该回答未通过引用后验校验（无有效页码锚点），请人工核对原文。"

    claims = llm_claims(answer, hits)
    return finish(answer, claims, "generative",
                  f"生成式 · {len(claims)} 条论断 · 引用已后验")


# ---------------- 工具：审计路径 ----------------

def graph_path(trace: list[dict]) -> str:
    return ">".join(dict.fromkeys(c.get("node", "?") for c in trace))
