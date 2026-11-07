"""Agent 类型化 State（LangGraph TypedDict）。

主图与 executor 子图共享键名（子图键 ⊆ 主图键），挂载时无需 schema 映射。
带 reducer 的键（trace/guardrails/evidence/raw_hits/branch_stats）在节点间累加。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

IntentType = Literal["qa_single", "qa_compare", "summarize", "graph_query", "writing", "chitchat"]


class Intent(TypedDict, total=False):
    type: IntentType
    aspects: list[str]          # 检索面（中文/英文随问题语言）
    language: str


class Step(TypedDict):
    paper_id: str
    query: str
    aspect: str


class Plan(TypedDict, total=False):
    steps: list[Step]
    mode: Literal["single", "compare", "summary"]


class AgentState(TypedDict, total=False):
    # 输入
    question: str
    paper_ids: list[str]
    # 路由 / 规划
    intent: Intent
    plan: Plan
    # 执行
    raw_hits: Annotated[list[dict[str, Any]], operator.add]   # 每路分支原始结果
    branch_stats: Annotated[list[dict[str, Any]], operator.add]
    evidence: list[dict[str, Any]]                            # fuse 后 top-8
    # 应答
    answer: str
    claims: list[dict[str, Any]]
    mode: Literal["generative", "extractive", "refusal", "none"]
    # 防线与轨迹
    guardrails: Annotated[list[str], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]
    retries: int                                              # verifier→planner 重规划计数


class ExecutorState(TypedDict, total=False):
    """Executor 子图独立 schema：不含父级 trace/guardrails，避免父图卡片经子图回流重复。"""
    question: str
    plan: Plan
    retries: int
    raw_hits: Annotated[list[dict[str, Any]], operator.add]
    branch_stats: Annotated[list[dict[str, Any]], operator.add]
    evidence: list[dict[str, Any]]
    trace: Annotated[list[dict[str, Any]], operator.add]      # 子图内部从零累积


def trace_card(node: str, title: str, summary: str, payload: dict | None = None) -> dict:
    """节点即卡片：一张执行卡（SSE trace 事件与 state.trace 共用此形状）。"""
    return {"node": node, "title": title, "summary": summary, "payload": payload or {}}
