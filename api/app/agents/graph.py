"""Meridian Agent 主图：Router → Planner → Executor(子图,并行) → Verifier → Recover → Responder。

- 主图 6 节点静态拓扑 + 显式条件边（无 Command 动态跳转）
- 回流边：verifier→recover→executor（证据不足重检索）、responder→recover→responder（降级/重生成）
- 失败恢复三策略统一经 recover 节点分发（详见 nodes.recover_node），预算：全局 ≤3，
  其中重检索 ≤2、格式重生成 ≤2；超预算走现有兜底出答案
- Executor 子图（独立 ExecutorState）：executor_entry →(Send 扇出)→ retrieve_task×N(并行) → executor_fuse；
  经 wrapper 节点手动 invoke，只把 evidence/branch_stats/子图自身 trace/failures/guardrails 传回父图
  （若直接挂编译子图，父级 trace 会经子图 reducer 回流造成卡片重复）
- build_graph() 供 FastAPI 与 LangGraph Studio（langgraph.json）共同加载
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import (
    _check_sufficiency, executor_entry_node, fan_out, fuse_node, planner_node,
    recover_node, responder_node, retrieve_task_node, router_node, verifier_node,
)
from .state import MAX_RECOVER, MAX_RE_RETRIEVE, AgentState, ExecutorState


def _after_router(state: AgentState) -> str:
    return "planner" if state.get("intent", {}).get("type") != "chitchat" else "responder"


def _after_verifier(state: AgentState) -> str:
    if _check_sufficiency(state):
        return "responder"
    if (state.get("recover_count", 0) >= MAX_RECOVER
            or state.get("re_retrieve_count", 0) >= MAX_RE_RETRIEVE):
        return "responder"  # 恢复预算尽：以现有证据作答或拒答
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
    """wrapper：以裁剪后的输入 invoke 子图，回传父图关心的键（trace 为子图自产，无回流）。"""
    out = _executor_app.invoke(
        {"question": state.get("question", ""), "plan": state.get("plan", {}),
         "retries": state.get("retries", 0)}, config)
    return {"evidence": out.get("evidence", []),
            "branch_stats": out.get("branch_stats", []),
            "trace": out.get("trace", []),
            "guardrails": out.get("guardrails", []),
            "failures": out.get("failures", [])}


def build_graph():
    g = StateGraph(AgentState)

    g.add_node("router", router_node)
    g.add_node("planner", planner_node)
    g.add_node("executor", executor_node)
    g.add_node("verifier", verifier_node)
    g.add_node("recover", recover_node)
    g.add_node("responder", responder_node)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", _after_router, ["planner", "responder"])
    g.add_edge("planner", "executor")
    g.add_edge("executor", "verifier")
    g.add_conditional_edges("verifier", _after_verifier, ["recover", "responder"])
    g.add_conditional_edges("recover", _recover_route, ["executor", "responder", END])
    g.add_conditional_edges("responder", _after_responder, ["recover", END])

    return g.compile()
