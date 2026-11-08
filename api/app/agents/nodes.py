"""LangGraph 状态图节点。

主图节点：router / planner / verifier / recover / responder（同步函数，LLM 复用 app/llm/client.py）。
recover 为失败恢复统一调度节点，三种策略：①证据不足重检索（改写查询+放宽 top_k→executor）
②工具失败降级（标记 degraded→responder 抽取式）③格式非法重生成（纠错反馈→responder 定向重写）。
Executor 子图节点：executor_entry(透传) → retrieve_task(Send 并行扇出，含 BM25 单通道降级) → executor_fuse(RRF)。
可视化：所有节点完成时经 config.configurable.queue 推送「节点即卡片」trace 事件；
state.trace 同步累积（最终随 final 事件整体下发，前端以其为准）。
协议 v1.1：另发结构化事件 plan / tool_call / tool_result / evidence / verifier
（右侧 Trace 时间线数据源），既有事件保持不变。
"""
from __future__ import annotations

import json
import queue as queue_mod
import time

from langgraph.types import Send

from ..llm import client as llm
from ..llm import prompts
from ..rag import indexing
from ..rag.answering import (
    NO_EVIDENCE,
    REFUSAL_CHITCHAT,
    WRITER_MIN_EVIDENCE,
    WRITER_REFUSAL,
    build_anchors,
    build_compare_messages,
    build_graph_messages,
    build_qa_messages,
    build_writer_messages,
    extractive_answer,
    format_context,
    hit_summary,
    llm_claims,
    near_evidence_block,
    validate_citations,
)
from ..rag.verify import verify_claims
from .state import (
    AGENT_INTENT,
    AGENT_LABEL,
    INTENT_AGENT,
    MAX_RECOVER,
    MAX_REGEN,
    READY_AGENTS,
    AgentState,
    Step,
    trace_card,
)

_GREETINGS = {"你好", "您好", "hi", "hello", "在吗", "你是谁", "测试", "hello world", "ping"}
_SUMMARY_HINTS = ("总结", "综述", "概括", "summary", "summarize", "主要讲", "讲了什么")


def _queue(config: dict | None) -> queue_mod.Queue | None:
    return ((config or {}).get("configurable") or {}).get("queue")


def _emit_event(config: dict | None, kind: str, data: dict) -> None:
    """向 SSE 队列推送协议事件（kind: trace/plan/tool_call/tool_result/evidence/verifier…）。"""
    q = _queue(config)
    if q is not None:
        q.put((kind, data))


def _emit(config: dict | None, card: dict) -> None:
    _emit_event(config, "trace", card)


def _plan_step(s: dict) -> dict:
    """plan 事件的步骤投影（前端时间线渲染所需最小字段）。"""
    return {"step_id": s.get("step_id", ""), "title": s.get("aspect") or s.get("query", ""),
            "aspect": s.get("aspect", ""), "paper_id": s.get("paper_id", ""),
            "query": s.get("query", "")}


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
    forced = (state.get("forced_agent") or "").strip().lower()
    forced_agent = forced if forced in AGENT_INTENT else ""

    intent: dict | None = None
    if forced_agent:
        # UI 强制指定：确定性路由，免 LLM 分类（现场演示有手动兜底）
        intent = {"type": AGENT_INTENT[forced_agent], "aspects": [question],
                  "language": "zh", "source": "forced"}
    elif llm.available():
        res = llm.chat(
            [{"role": "system", "content":
                "你是意图路由器。把用户问题分类为以下之一：qa_single(单篇细节问答)、qa_compare(多论文对比)、"
                "summarize(总结/综述)、graph_query(引用关系/研究脉络)、writing(写作请求)、"
                "viz(把论文中的数据画成图表)、chitchat(寒暄/无关)。"
                '输出 JSON：{"type":"...","aspects":["面向检索的子问题1","子问题2"]}。'
                "aspects 为 2~4 个可独立检索的子问题（chitchat 时为空数组），使用与问题相同的语言。"},
             {"role": "user", "content": f"库内论文数：{len(paper_ids)}\n问题：{question}"}],
            json_mode=True, temperature=0.0, module="router", audit_prompt=question[:200],
        )
        data = llm.parse_json(res.get("text", ""))
        if data and data.get("type") in {"qa_single", "qa_compare", "summarize",
                                         "graph_query", "writing", "viz", "chitchat"}:
            aspects = [a for a in (data.get("aspects") or []) if isinstance(a, str) and a.strip()]
            if data["type"] != "chitchat" and not aspects:
                aspects = [question]
            intent = {"type": data["type"], "aspects": aspects, "language": "zh",
                      "source": "llm"}
        else:
            intent = None  # 解析失败 → 规则兜底
    if intent is None:
        intent = _rule_intent(question, paper_ids)

    # Agent 分发（INTENT_AGENT 路由表为单一事实源）；未实装 Agent 降级到 Reader 并如实标注
    agent: str = "none" if intent["type"] == "chitchat" else INTENT_AGENT.get(intent["type"], "reader")
    guardrails: list[str] = []
    handoffs: list[dict] = []
    if agent != "none" and agent not in READY_AGENTS:
        guardrails.append(f"agent_unready:{agent}")
        handoffs.append({"from": "router", "to": "reader",
                         "note": f"{AGENT_LABEL[agent]} 未实装 → 降级 Reader 执行"})
        agent = "reader"
    if agent != "none":
        note = ("UI 强制指定" if forced_agent else
                {"llm": "LLM 意图分类", "" : "规则兜底分类"}.get(intent.get("source", ""), "规则分类"))
        handoffs.append({"from": "router", "to": agent,
                         "note": f"{intent['type']} → {AGENT_LABEL[agent]}（{note}）"})

    card = trace_card("router", "意图识别 + Agent 分发",
                      f"{intent['type']} → {AGENT_LABEL[agent]} · {len(intent['aspects'])} 个检索面",
                      {"intent": intent, "agent": agent})
    _emit(config, card)
    return {"intent": intent, "agent": agent, "language": intent.get("language", "zh"),
            "trace": [card], "handoffs": handoffs, "guardrails": guardrails}


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

    # step_id 分配（Trace 时间线）：round = retries+1；重检索轮由 recover① 另行分配
    round_no = retries + 1
    for i, s in enumerate(steps, 1):
        s["step_id"] = f"s{round_no}-{i}"
    plan = {"steps": steps, "mode": mode}
    title = "任务规划" + ("（重规划）" if retries > 0 else "")
    card = trace_card("planner", title, f"{len(steps)} 步 · {len(paper_ids)} 篇 × {len(aspects)} 面",
                      {"steps": steps})
    _emit(config, card)
    _emit_event(config, "plan", {"round": round_no, "mode": mode,
                                 "steps": [_plan_step(s) for s in steps]})
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
    top_k = 10 + retries * 6  # 重检索时放宽召回
    step_id = step.get("step_id", "")
    t_call = time.time()
    _emit_event(config, "tool_call", {"step_id": step_id, "tool": "kb_search",
                                      "args": {"paper_id": step["paper_id"],
                                               "query": step["query"], "top_k": top_k}})
    guardrails: list[str] = []
    failures: list[dict] = []
    try:
        search = indexing.hybrid_search([step["paper_id"]], step["query"], top_k=top_k)
    except Exception as e:  # noqa: BLE001 —— 策略②：混合检索故障 → BM25 单通道降级
        hits = indexing.fts_search([step["paper_id"]], step["query"], top_k=top_k)
        search = {"hits": hits, "channels": ["bm25"], "bm25_count": len(hits), "dense_count": 0}
        guardrails.append("retrieval_degraded:bm25_only")
        failures.append({"node": "retrieve_task", "kind": "tool_failure",
                         "detail": f"hybrid_search:{type(e).__name__}:{e}"[:120]})
    tagged = [{**h, "aspect": step["aspect"], "query": step["query"]} for h in search["hits"]]
    stat = {"paper_id": step["paper_id"], "query": step["query"], "aspect": step["aspect"],
            "hits": len(tagged), "channels": search["channels"],
            "bm25": search["bm25_count"], "dense": search["dense_count"]}
    degraded = "（BM25 降级）" if guardrails else ""
    card = trace_card("retrieve_task", "并行检索",
                      f"{step['query'][:26]}… → {len(tagged)} 块{degraded}" if len(step["query"]) > 26
                      else f"{step['query']} → {len(tagged)} 块{degraded}",
                      stat)
    _emit(config, card)
    _emit_event(config, "tool_result", {
        "step_id": step_id, "ok": True,
        **({"degraded": "bm25_only"} if guardrails else {}),
        "summary": f"{step['query']} → {len(tagged)} 块{degraded}",
        "refs": [h.get("chunk_id", "") for h in tagged],
        "elapsed_ms": int((time.time() - t_call) * 1000),
        "payload": {"hits": len(tagged), "channels": search["channels"],
                    "bm25": search["bm25_count"], "dense": search["dense_count"]}})
    return {"raw_hits": tagged, "branch_stats": [stat], "trace": [card],
            "guardrails": guardrails, "failures": failures}


def fuse_node(state: AgentState, config=None) -> dict:
    results = state.get("raw_hits", [])
    guardrails: list[str] = []
    if results:
        # 每个 (aspect, paper) 分支是一路检索，跨分支 RRF 融合去重
        groups: dict[tuple[str, str], list[dict]] = {}
        for h in results:
            groups.setdefault((h.get("aspect", ""), h.get("paper_id", "")), []).append(h)
        fused = indexing.rrf_fuse(*[(f"b{i}", lst) for i, lst in enumerate(groups.values())],
                                  top_k=20)
        # 四层防御①最后一环：bge-reranker 全局精排（每问 1 次调用），失败降级回融合排序
        fused_final: list[dict] | None = None
        if indexing.rerank_available():
            fused_final = indexing.rerank_hits(state.get("question", ""), fused, top_k=8)
            if fused_final is None:
                guardrails.append("rerank_degraded")
        else:
            fused_final = None
        if fused_final:
            fused = fused_final
            rerank_note = "rerank 已精排"
        else:
            fused = fused[:8]
            rerank_note = "rerank 降级/未配置 → 融合排序截断"
    else:
        fused = []
        rerank_note = ""
    channels = sorted({c for b in state.get("branch_stats", []) for c in b.get("channels", [])})
    card = trace_card("executor_fuse", "证据融合",
                      f"RRF · {len(results)} 路原始 → {len(fused)} 块证据 · 通道 {'+'.join(channels) or 'bm25'} · {rerank_note}",
                      {"channels": channels, "branch_count": len(state.get("branch_stats", [])),
                       "reranked": bool(fused and fused[0].get("rerank") is not None)})
    _emit(config, card)
    _emit_event(config, "evidence", {
        "channels": channels,
        "bm25_count": sum(b.get("bm25", 0) for b in state.get("branch_stats", [])),
        "dense_count": sum(b.get("dense", 0) for b in state.get("branch_stats", [])),
        "hits": hit_summary(fused)})
    out: dict = {"evidence": fused, "trace": [card]}
    if guardrails:
        out["guardrails"] = guardrails
    return out


# ---------------- Verifier ----------------

def _check_sufficiency(state: AgentState) -> bool:
    """证据充分性：每个检索面至少 1 条有效命中；compare 另要求每篇论文覆盖；graph 走元数据证据。"""
    if state.get("agent") == "graph":
        return True  # Graph Agent 的证据是 references 元数据（graph_build 产出），不以 chunk 覆盖论充分
    plan = state.get("plan", {})
    evidence = state.get("evidence", [])
    if not evidence:
        return False
    aspects = {s.get("aspect", "") for s in plan.get("steps", [])}
    if aspects:
        hit_aspects = {h.get("aspect", "") for h in evidence}
        if not aspects <= hit_aspects:
            return False
    if plan.get("mode") == "compare":
        # Compare Guardrail：防止一篇论文的证据占满上下文、另一篇零命中被"脑补对比"
        planned_papers = {s.get("paper_id", "") for s in plan.get("steps", [])}
        hit_papers = {h.get("paper_id", "") for h in evidence}
        return planned_papers <= hit_papers
    return True


def verifier_node(state: AgentState, config=None) -> dict:
    ok = _check_sufficiency(state)
    evidence_count = len(state.get("evidence", []))
    if ok:
        card = trace_card("verifier", "证据校验", f"通过 · {evidence_count} 块覆盖全部检索面",
                          {"sufficient": True})
        _emit(config, card)
        _emit_event(config, "verifier", {"verdict": "pass", "missing": [],
                                         "evidence_count": evidence_count,
                                         "action": "respond"})
        return {"trace": [card]}
    missing = {s.get("aspect", "") for s in state.get("plan", {}).get("steps", [])} - \
              {h.get("aspect", "") for h in state.get("evidence", [])}
    missing_sorted = sorted(missing)
    card = trace_card("verifier", "证据校验",
                      f"不足 · 缺少 {len(missing_sorted)} 个检索面 → 交由 recover 恢复",
                      {"sufficient": False, "missing": missing_sorted})
    _emit(config, card)
    _emit_event(config, "verifier", {"verdict": "insufficient", "missing": missing_sorted,
                                     "evidence_count": evidence_count, "action": "recover"})
    return {"pending_failure": {"kind": "insufficient_evidence", "missing": missing_sorted,
                                "node": "verifier"},
            "failures": [{"node": "verifier", "kind": "insufficient_evidence",
                          "detail": f"missing:{','.join(missing_sorted)}"[:120]}],
            "trace": [card]}


# ---------------- Recover（失败恢复统一调度，第 6 节点） ----------------

_REGEN_FEEDBACK = (
    "你上一轮的回答未通过引用格式校验（即将进行第 {n} 次生成）。问题：{problems}。\n"
    "请重新生成完整回答：内容与结论保持不变，为每个事实句补上引用标记，"
    "格式严格为 [E编号]，编号只能取自本次证据包：{valid_ids}。"
)


def _rewrite_aspects(missing: list[str], question: str) -> dict[str, str]:
    """策略①：为零命中检索面生成替换查询（LLM 换措辞补术语；失败回退原检索面文本）。"""
    if llm.available() and missing:
        res = llm.chat(
            [{"role": "system", "content":
                "你是检索恢复器。以下检索面在全文检索中零命中，请为每个生成一个替换查询："
                "换一种措辞、使用论文正文中更可能出现的术语（如中文面给英文术语面），不要复述原句。"
                '输出 JSON：{"rewrites":{"原检索面":"新查询",...}}，键必须与输入检索面一一对应。'},
             {"role": "user", "content": "检索面：\n" + "\n".join(f"- {m}" for m in missing) +
              f"\n原问题：{question}"}],
            json_mode=True, temperature=0.5, module="recover", audit_prompt=question[:200],
        )
        data = llm.parse_json(res.get("text", ""))
        rewrites = data.get("rewrites") if isinstance(data, dict) else None
        if isinstance(rewrites, dict) and any(str(rewrites.get(m, "")).strip() for m in missing):
            return {m: str(rewrites.get(m, m)).strip() or m for m in missing}
    return {m: m for m in missing}  # 规则回退：用回原始措辞（规划器改写可能偏了）


def recover_node(state: AgentState, config=None) -> dict:
    """按 pending_failure 分发三种恢复策略，路由决策写入 recover_route（条件边读取）：
    ① insufficient_evidence → 改写缺失检索面 + 放宽 top_k → executor 重检索
    ② tool_failure          → 标记 degraded → responder 抽取式降级
    ③ format_invalid        → 组装纠错反馈 retry_prompt → responder 定向重生成
    """
    pend = state.get("pending_failure") or {}
    kind = pend.get("kind", "")
    recover_count = state.get("recover_count", 0) + 1
    plan_evt: dict | None = None   # 策略① 产生新计划时，在恢复卡片后补发 plan 事件
    upd: dict = {"pending_failure": None, "recover_count": recover_count,
                 "recover_route": "responder"}

    if kind == "insufficient_evidence":
        # ---- 策略①：证据不足重检索 ----
        missing = [m for m in pend.get("missing", []) if m]
        re_retrieve = state.get("re_retrieve_count", 0) + 1
        rewrites = _rewrite_aspects(missing, state.get("question", ""))
        old_aspects = list(dict.fromkeys(
            s.get("aspect", "") for s in state.get("plan", {}).get("steps", []))) or missing
        steps = [{"paper_id": pid, "query": rewrites.get(a, a), "aspect": a}
                 for a in old_aspects for pid in state.get("paper_ids", [])]
        retries = state.get("retries", 0) + 1
        round_no = retries + 1  # 新一轮步骤 id：s{round}-*
        for i, s in enumerate(steps, 1):
            s["step_id"] = f"s{round_no}-{i}"
        upd.update({"plan": {"steps": steps,
                             "mode": state.get("plan", {}).get("mode", "single")},
                    "retries": retries, "re_retrieve_count": re_retrieve,
                    "recover_route": "executor",
                    "guardrails": [f"insufficient_evidence:{re_retrieve}"]})
        card = trace_card("recover", "恢复·重检索",
                          f"#{recover_count} · 改写 {len(missing)} 个检索面 · top_k → {10 + retries * 6}",
                          {"strategy": "re_retrieve", "missing": missing, "rewrites": rewrites})
        plan_evt = {"round": round_no, "mode": state.get("plan", {}).get("mode", "single"),
                    "steps": [_plan_step(s) for s in steps]}

    elif kind == "tool_failure":
        # ---- 策略②：工具失败降级 ----
        detail = pend.get("detail", "工具调用失败")
        upd.update({"degraded": True, "guardrails": ["tool_failure_degraded"]})
        card = trace_card("recover", "恢复·降级",
                          f"#{recover_count} · {detail} → 抽取式证据模式",
                          {"strategy": "degrade", "detail": detail})

    elif kind == "format_invalid":
        # ---- 策略③：格式非法重生成（带错误反馈；预算尽则警告放行收尾） ----
        regen = state.get("regen_count", 0)
        invalid_eids = pend.get("invalid_eids", [])
        eid_map = {i: h for i, h in enumerate(state.get("evidence", []), 1)}
        if regen >= MAX_REGEN:
            answer = state.get("answer", "") + \
                "\n\n> ⚠️ 该回答未通过引用后验校验（多次重生成仍失败），请人工核对原文。"
            upd.update({"answer": answer, "recover_route": "end",
                        "guardrails": ["format_accept_with_warning"]})
            card = trace_card("recover", "恢复·重生成",
                              f"#{recover_count} · 重生成预算用尽 → 警告放行",
                              {"strategy": "regen_exhausted"})
        else:
            valid_ids = ", ".join(f"E{eid}(p{h['page'] + 1})"
                                  for eid, h in sorted(eid_map.items())) or "无"
            problems = [f"引用了证据中不存在的编号 {'、'.join(invalid_eids)}"
                        if invalid_eids else "完全缺少 [E编号] 引用标记"]
            prompt = _REGEN_FEEDBACK.format(n=regen + 1, problems="；".join(problems),
                                            valid_ids=valid_ids)
            upd.update({"retry_prompt": prompt, "regen_count": regen + 1,
                        "guardrails": [f"format_regen:{regen + 1}"]})
            card = trace_card("recover", "恢复·重生成",
                              f"#{recover_count} · 注入纠错反馈 · 第 {regen + 1}/{MAX_REGEN} 次"
                              + (f" · 非法编号 {invalid_eids}" if invalid_eids else " · 缺引用"),
                              {"strategy": "regen", "invalid_eids": invalid_eids,
                               "valid_ids": sorted(eid_map)})
    else:
        card = trace_card("recover", "恢复", f"#{recover_count} · 未知失败类型 → 继续",
                          {"strategy": "noop"})

    upd["failures"] = [{"node": "recover", "kind": kind or "unknown",
                        "detail": card["summary"][:120]}]
    upd["trace"] = [card]
    _emit(config, card)
    if plan_evt is not None:
        _emit_event(config, "plan", plan_evt)
    return upd


# ---------------- Responder ----------------

def responder_node(state: AgentState, config=None) -> dict:
    q = _queue(config)
    question = state["question"]
    intent_type = state.get("intent", {}).get("type", "qa_single")
    agent = state.get("agent", "reader")
    retry_prompt = state.get("retry_prompt", "")
    prev_answer = state.get("answer", "")
    guardrails: list[str] = []
    is_compare = agent == "compare"

    def finish(answer: str, claims: list[dict], mode: str, summary: str) -> dict:
        card = trace_card("responder", "生成回答", summary,
                          {"mode": mode, "claims": len(claims), "agent": agent})
        _emit(config, card)
        out: dict = {"answer": answer, "claims": claims, "mode": mode,
                     "guardrails": guardrails, "trace": [card], "retry_prompt": "",
                     "anchors": build_anchors({i: h for i, h in
                                               enumerate(state.get("evidence", []), 1)})}
        if is_compare:
            out["compare_table"] = state.get("compare_table") or {}
        return out

    if intent_type == "chitchat":
        return finish(REFUSAL_CHITCHAT, [], "refusal", "寒暄拒答（范围外）")

    # Agent 范围 Guardrail 拒绝（compare_entry 单篇转介 / viz 无表拒绝等）：透传收尾
    if state.get("mode") == "refusal" and prev_answer and not retry_prompt:
        return finish(prev_answer, [], "refusal", f"{AGENT_LABEL.get(agent, agent)} · 范围校验拒绝")

    # Viz：不走 chunk 证据管线，chart_spec 即产物（viz_extract 已保证其存在或已拒绝）
    if agent == "viz":
        spec = state.get("chart_spec") or {}
        if not spec:
            return finish(NO_EVIDENCE, [], "none", "无图表数据")
        prov = spec.get("provenance") or {}
        n_points = len(spec.get("data") or [])
        answer = (f"已生成 **{spec.get('type', 'bar')} 图**（{n_points} 个数据点）："
                  f"数据来源 {prov.get('paper_id', '')} 第 {prov.get('page', 1)} 页"
                  f" 表格 {prov.get('table_id', '')}。"
                  f"横轴「{spec.get('x_label', '')}」，纵轴「{spec.get('y_label', '')}」。"
                  "图表已随本回答下发，可在下方查看。")
        if llm.available() and not retry_prompt:
            try:
                res = llm.chat(
                    [{"role": "system", "content":
                        "你是图表解读助手。只依据给出的 chart spec 数据写 ≤80 字的客观解读，"
                        "禁止引入 spec 之外的信息。"},
                     {"role": "user", "content": json.dumps(spec, ensure_ascii=False)}],
                    max_tokens=200, module="viz", audit_prompt=question[:100])
                insight = (res.get("text") or "").strip()
                if insight:
                    answer += "\n\n简要解读：" + insight
            except Exception:  # noqa: BLE001, S110 —— 解读失败不影响图表交付
                pass
        return finish(answer, [], "generative" if llm.available() else "extractive",
                      f"出图 · {spec.get('type')} · {n_points} 点 · 来源已标注")

    hits = state.get("evidence", [])
    if not hits and agent != "graph":
        return finish(NO_EVIDENCE, [], "none", "无证据 → 明确拒答")

    # Writer Guardrail：证据池不足不开写（Evidence-First）
    if agent == "writer" and len(hits) < WRITER_MIN_EVIDENCE:
        guardrails.append("writer_insufficient_evidence")
        return finish(WRITER_REFUSAL.format(n=len(hits), m=WRITER_MIN_EVIDENCE),
                      [], "refusal", f"Writer · 证据池 {len(hits)}/{WRITER_MIN_EVIDENCE} → 拒写")

    eid_map = format_context(hits)[1]

    # Graph：图谱证据在元数据层，无 LLM/降级时输出确定性指标摘要（不冒充生成）
    if agent == "graph" and (state.get("degraded") or not llm.available()):
        gd = state.get("graph_data") or {}
        metrics = gd.get("metrics") or {}
        lines = [(f"引用图谱摘要（指标 backend：{metrics.get('backend', 'none')}）："
                  f"库内节点 {len(gd.get('nodes', []))} 篇 · 引用边 {metrics.get('edge_count', 0)} 条 · "
                  f"外部引用 {gd.get('external_count', 0)} 条（未匹配库内论文）。")]
        for i, t in enumerate(metrics.get("nodes", [])[:3], 1):
            lines.append(f"{i}. {t.get('title', t.get('paper_id', ''))} — "
                         f"PageRank {t.get('pagerank', 0)} · 被引 {t.get('in_degree', 0)} · "
                         f"引出 {t.get('out_degree', 0)}")
        return finish("\n".join(lines), [], "extractive",
                      f"图谱指标摘要 · backend {metrics.get('backend', 'none')}")

    # 降级路径（策略②）：recover 标记 degraded → 抽取式证据模式
    if state.get("degraded"):
        answer, claims = extractive_answer(question, hits)
        return finish(answer, claims, "extractive",
                      f"工具失败降级 → 抽取式 · {len(claims)} 条论断")

    if not llm.available():
        answer, claims = extractive_answer(question, hits)
        guardrails.append("llm_unavailable_fallback")
        return finish(answer, claims, "extractive",
                      f"抽取式降级 · {len(claims)} 条论断")

    # 生成式（含策略③重生成）：按 Agent 选合成通道 → 流式输出 → 引用后验 → ClaimTrace
    language = state.get("language") or "zh"   # 语言边界 guardrail（4.5）
    _slot_by_module = {"compare": ("compare", "system_compare"),
                       "writer": ("writer", "system_writer"),
                       "graph": ("graph", "system_graph")}
    pv_module, pv_slot = _slot_by_module.get(agent if not is_compare else "compare",
                                             ("qa", "system_qa"))
    prompt_version = prompts.prompt_version(pv_module, pv_slot)
    if is_compare:
        messages = build_compare_messages(question, state.get("compare_table"), hits,
                                          language=language)
    elif agent == "writer":
        # Writer：大纲先行（artifact 下发），正文随后按同一大纲流式生成
        aspects = list(dict.fromkeys(
            s.get("aspect") or s.get("query", "") for s in state.get("plan", {}).get("steps", [])))
        outline = {"title": question[:40],
                   "sections": [a for a in aspects if a][:5] or ["背景", "要点", "结论"]}
        _emit_event(config, "artifact", {"kind": "outline", "outline": outline})
        messages = build_writer_messages(question, outline, hits, language=language)
    elif agent == "graph":
        messages = build_graph_messages(state.get("graph_data", {}).get("metrics", {}),
                                        question, hits, language=language)
    else:
        messages = build_qa_messages(question, hits, language=language)
    if retry_prompt and prev_answer:
        # recover 注入的纠错反馈：上一轮答案 + 具体违规点，定向修复而非盲重试
        messages += [{"role": "assistant", "content": prev_answer},
                     {"role": "user", "content": retry_prompt}]
    parts: list[str] = []
    streamed = False
    try:
        for piece in llm.stream_chat(messages, max_tokens=1500, module=pv_module,
                                     audit_prompt=question, prompt_version=prompt_version):
            streamed = True
            parts.append(piece)
            if q is not None:
                q.put(("delta", {"text": piece}))
    except Exception:  # noqa: BLE001 —— 流中断走同步兜底
        streamed = False
    answer = "".join(parts)
    if not streamed or not answer.strip():
        res = llm.chat(messages, max_tokens=1500, module=pv_module, audit_prompt=question,
                       prompt_version=prompt_version)
        answer = res.get("text", "")
        if answer and q is not None:
            q.put(("delta", {"text": answer}))

    if not answer.strip():
        # 策略②：生成工具失败 → 交 recover 降级；恢复预算尽则就地抽取式
        if state.get("recover_count", 0) < MAX_RECOVER:
            card = trace_card("responder", "生成回答", "生成失败（空响应）→ 请求恢复降级",
                              {"mode": "pending_degrade"})
            _emit(config, card)
            return {"pending_failure": {"kind": "tool_failure", "detail": "LLM 空响应/调用失败"},
                    "failures": [{"node": "responder", "kind": "tool_failure",
                                  "detail": "llm_empty_response"}],
                    "guardrails": ["llm_call_failed"], "trace": [card],
                    "retry_prompt": ""}
        answer, claims = extractive_answer(question, hits)
        guardrails.append("tool_failure_degraded")
        return finish(answer, claims, "extractive", "生成失败且恢复预算用尽 → 就地抽取式")

    answer, cited_guards = validate_citations(answer, eid_map)
    guardrails += cited_guards

    # 证据不足拒答：不进重写环（合法无引用），按拒答策略附「最接近的 3 条证据」
    if "未找到依据" in answer:
        answer += near_evidence_block(hits)
        return finish(answer, [], "refusal", "证据不足拒答 · 已附最近证据供人工判断")

    invalid = [g for g in cited_guards
               if g == "no_citation_found" or g.startswith("invalid_citation:E")]
    if invalid:
        # 策略③：格式非法 → 交 recover 定向重生成；预算尽则警告放行
        if (state.get("regen_count", 0) < MAX_REGEN
                and state.get("recover_count", 0) < MAX_RECOVER):
            invalid_eids = [f"E{g.split(':E', 1)[1]}" for g in cited_guards
                            if g.startswith("invalid_citation:E")]
            card = trace_card("responder", "生成回答",
                              f"引用校验未通过（{len(invalid)} 项）→ 请求定向重生成",
                              {"mode": "pending_regen", "invalid": invalid})
            _emit(config, card)
            return {"answer": answer, "mode": "generative",
                    "pending_failure": {"kind": "format_invalid",
                                        "invalid_eids": invalid_eids},
                    "failures": [{"node": "responder", "kind": "format_invalid",
                                  "detail": ";".join(invalid)[:120]}],
                    "guardrails": guardrails, "trace": [card], "retry_prompt": ""}
        guardrails.append("format_accept_with_warning")
        answer += "\n\n> ⚠️ 该回答未通过引用后验校验（无有效证据编号锚点），请人工核对原文。"

    claims, eid_map_claims = llm_claims(answer, hits)
    # 四层防御④：L2 引文子串 / L3 数值实体 / L4 语义 judge（L1 已在 validate_citations 完成）
    claims, verify_guards = verify_claims(claims, eid_map_claims)
    guardrails += verify_guards

    def verify_note() -> str:
        n_check = sum(1 for c in claims if c.get("status") == "needs_check")
        n_unsup = sum(1 for c in claims if c.get("status") == "unsupported")
        note = " · L1-L4 校验完成"
        if n_check:
            note += f" · {n_check} 条需核对"
        if n_unsup:
            note += f" · {n_unsup} 条无支撑"
        return note

    if is_compare:
        return finish(answer, claims, "generative",
                      f"对比合成 · {len(claims)} 条论断{verify_note()} · 维度表已下发")
    if agent == "writer":
        # P0-6：final 必须携带 outline（runner._push_success 有透传、前端 OutlineView 就绪，
        # 旧代码只发 artifact 事件——刷新后前端拿不到大纲 = 断链）
        return {**finish(answer, claims, "generative",
                         f"写作完成 · {len(claims)} 条论断{verify_note()} · 大纲已下发"),
                "outline": outline}
    if agent == "graph":
        return finish(answer, [], "generative",
                      "图谱综述 · 指标来自 references 元数据真实计算")
    return finish(answer, claims, "generative",
                  f"生成式 · {len(claims)} 条论断{verify_note()}")


# ---------------- 工具：审计路径 ----------------

def graph_path(trace: list[dict]) -> str:
    return ">".join(dict.fromkeys(c.get("node", "?") for c in trace))
