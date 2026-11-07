"""Meridian 多智能体主图（五 Agent 全实装）：
Router(分发) → Agent入口 → [Planner → Executor(子图,并行) → Verifier → Agent产物节点] → Recover → Responder。

- 主图静态拓扑 + 显式条件边（无 Command 动态跳转）
- 多智能体分发：router 按 INTENT_AGENT 路由表选 Agent（UI 可 forced_agent 强制指定），
  五个专属入口节点带范围 Guardrail（compare 单篇拒绝 / viz 无表拒绝 / reader 范围锁定）；
  Agent 专属产物节点：align（对比表）/ graph_build（引用图+指标）/ viz_extract（chart_spec），
  均经 artifact 事件下发前端；writer 无产物节点（大纲在 responder 流式前下发）
- 管线差异：reader/compare/writer/graph 走共享检索管线（planner→executor→verifier→recover⇄），
  viz 走专用管线（viz_entry→viz_extract，不查 chunk，只走 Document IR 表格）
- 回流边：verifier→recover→executor（证据不足重检索）、responder→recover→responder（降级/重生成）
- 失败恢复三策略统一经 recover 节点分发（详见 nodes.recover_node），预算：全局 ≤3，
  其中重检索 ≤2、格式重生成 ≤2；超预算走现有兜底出答案（compare/graph 仍出部分产物）
- Executor 子图（独立 ExecutorState）：executor_entry →(Send 扇出)→ retrieve_task×N(并行) → executor_fuse；
  经 wrapper 节点手动 invoke，只把 evidence/branch_stats/子图自身 trace/failures/guardrails 传回父图
  （若直接挂编译子图，父级 trace 会经子图 reducer 回流造成卡片重复）
- build_graph() 供 FastAPI 与 LangGraph Studio（langgraph.json）共同加载；
  可选 checkpointer（SqliteSaver，见 checkpointer.py）启用断点续跑
- LLM/易碎节点挂 RetryPolicy（仅瞬时基础设施错误自动重试；业务级失败仍走 recover），
  并经 _debug.failable 包装支持 SCHOLAR_FAIL_AT 故障注入
"""
from __future__ import annotations

import sqlite3

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from ._debug import failable
from .nodes import (
    _check_sufficiency, executor_entry_node, fan_out, fuse_node, planner_node,
    recover_node, responder_node, retrieve_task_node, router_node, verifier_node,
)
from .specialists import (
    align_node, compare_entry_node, graph_build_node, graph_entry_node,
    reader_entry_node, viz_entry_node, viz_extract_node, writer_entry_node,
)
from .state import MAX_RECOVER, MAX_RE_RETRIEVE, AgentState, ExecutorState

# 瞬时基础设施错误（SQLite 锁竞争/网络超时）自动重试；LLM 业务级失败由 recover 节点统一恢复
_RETRY = RetryPolicy(max_attempts=3, backoff_factor=2.0,
                     retry_on=(sqlite3.OperationalError, TimeoutError, ConnectionError))

_AGENT_ENTRY = {"reader": "reader_entry", "compare": "compare_entry",
                "graph": "graph_entry", "writer": "writer_entry", "viz": "viz_entry"}


def _after_router(state: AgentState) -> str:
    """分发：chitchat 直达 responder；其余按 router 选定的 Agent 走专属入口。"""
    if state.get("intent", {}).get("type") == "chitchat":
        return "responder"
    return _AGENT_ENTRY.get(state.get("agent", "reader"), "reader_entry")


def _after_specialist_entry(state: AgentState) -> str:
    """入口 Guardrail 路由：入口拒绝（mode=refusal）→ responder 收尾；viz 正常 → 专用管线；
    其余 → planner 走共享检索管线。"""
    if state.get("mode") == "refusal":
        return "responder"
    return "viz_extract" if state.get("agent") == "viz" else "planner"


def _specialist_exit(state: AgentState) -> str:
    """Agent 专属产物节点 → responder（recover 环重检索后同样经此收尾）。"""
    return "responder"


def _after_verifier(state: AgentState) -> str:
    agent = state.get("agent", "reader")
    if _check_sufficiency(state):
        return "align" if agent == "compare" else \
               "graph_build" if agent == "graph" else "responder"
    if (state.get("recover_count", 0) >= MAX_RECOVER
            or state.get("re_retrieve_count", 0) >= MAX_RE_RETRIEVE):
        # 恢复预算尽：compare 仍过 align 出部分对比表、graph 过 graph_build 出指标（缺额如实呈现）
        return "align" if agent == "compare" else \
               "graph_build" if agent == "graph" else "responder"
    return "recover"


def _after_responder(state: AgentState) -> str:
    if state.get("pending_failure") and state.get("recover_count", 0) < MAX_RECOVER:
        return "recover"
    return END


def _recover_route(state: AgentState) -> str:
    route = state.get("recover_route", "responder")
    return END if route == "end" else route


def _build_executor():
    g = StateGraph(ExecutorState)
    g.add_node("executor_entry", executor_entry_node)
    g.add_node("retrieve_task", retrieve_task_node)
    g.add_node("executor_fuse", fuse_node)
    g.add_edge(START, "executor_entry")
    g.add_conditional_edges("executor_entry", fan_out,
                            ["retrieve_task", "executor_fuse"])
    g.add_edge("retrieve_task", "executor_fuse")
    g.add_edge("executor_fuse", END)
    return g.compile()


_executor_app = _build_executor()


def executor_node(state: AgentState, config=None) -> dict:
    """wrapper：以裁剪后的输入 invoke 子图，回传父图关心的键（trace 为子图自产，无回流）。

    透传 config 时 configurable 白名单只保留 queue：父图（带 SqliteSaver）会经 config
    私有键向子图传播 checkpointer，若带入，子图将以无 thread_id 的 config 调
    get_tuple 而抛 KeyError；子图仅需 queue（节点卡片 _emit 通道）。
    """
    cfg = dict(config or {})
    q = (cfg.get("configurable") or {}).get("queue")
    cfg["configurable"] = {"queue": q} if q is not None else {}
    out = _executor_app.invoke(
        {"question": state.get("question", ""), "plan": state.get("plan", {}),
         "retries": state.get("retries", 0)}, cfg)
    return {"evidence": out.get("evidence", []),
            "branch_stats": out.get("branch_stats", []),
            "trace": out.get("trace", []),
            "guardrails": out.get("guardrails", []),
            "failures": out.get("failures", [])}


def build_graph(checkpointer=None):
    g = StateGraph(AgentState)

    g.add_node("router", failable("router", router_node), retry_policy=_RETRY)
    g.add_node("reader_entry", reader_entry_node)
    g.add_node("compare_entry", compare_entry_node)
    g.add_node("graph_entry", graph_entry_node)
    g.add_node("writer_entry", writer_entry_node)
    g.add_node("viz_entry", viz_entry_node)
    g.add_node("planner", failable("planner", planner_node), retry_policy=_RETRY)
    g.add_node("executor", executor_node)
    g.add_node("verifier", failable("verifier", verifier_node), retry_policy=_RETRY)
    g.add_node("align", align_node)
    g.add_node("graph_build", graph_build_node)
    g.add_node("viz_extract", viz_extract_node)
    g.add_node("recover", failable("recover", recover_node), retry_policy=_RETRY)
    g.add_node("responder", failable("responder", responder_node), retry_policy=_RETRY)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", _after_router,
                            ["reader_entry", "compare_entry", "graph_entry",
                             "writer_entry", "viz_entry", "responder"])
    g.add_edge("reader_entry", "planner")
    g.add_edge("graph_entry", "planner")
    g.add_edge("writer_entry", "planner")
    g.add_conditional_edges("compare_entry", _after_specialist_entry, ["planner", "responder"])
    g.add_conditional_edges("viz_entry", _after_specialist_entry, ["viz_extract", "responder"])
    g.add_edge("planner", "executor")
    g.add_edge("executor", "verifier")
    g.add_conditional_edges("verifier", _after_verifier,
                            ["recover", "align", "graph_build", "responder"])
    g.add_edge("align", "responder")
    g.add_edge("graph_build", "responder")
    g.add_edge("viz_extract", "responder")
    g.add_conditional_edges("recover", _recover_route, ["executor", "responder", END])
    g.add_conditional_edges("responder", _after_responder, ["recover", END])

    return g.compile(checkpointer=checkpointer)
