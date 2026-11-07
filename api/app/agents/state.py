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
    retries: int                                              # 重检索轮次（retrieve_task 放宽 top_k）
    # 失败恢复（recover 统一调度，三种策略见 recover_node）
    failures: Annotated[list[dict[str, Any]], operator.add]   # 失败审计日志（只增不改）
    pending_failure: dict[str, Any] | None                    # 待恢复失败（recover 消费后置 None）
    recover_route: str                                        # recover 的路由决策（executor/responder/end）
    recover_count: int                                        # 全局恢复预算（≤MAX_RECOVER）
    re_retrieve_count: int                                    # 策略①证据不足重检索计数
    regen_count: int                                          # 策略③格式非法重生成计数
    retry_prompt: str                                         # recover 组装的纠错反馈指令
    degraded: bool                                            # 策略②工具失败降级标记（→抽取式）


class ExecutorState(TypedDict, total=False):
    """Executor 子图独立 schema：不含父级 trace，避免父图卡片经子图回流重复；
    failures/guardrails 为子图自产（工具降级），经 wrapper 回传父图。"""
    question: str
    plan: Plan
    retries: int
    raw_hits: Annotated[list[dict[str, Any]], operator.add]
    branch_stats: Annotated[list[dict[str, Any]], operator.add]
    evidence: list[dict[str, Any]]
    trace: Annotated[list[dict[str, Any]], operator.add]      # 子图内部从零累积
    guardrails: Annotated[list[str], operator.add]
    failures: Annotated[list[dict[str, Any]], operator.add]


# 恢复预算：全局 ≤3 次；其中证据不足重检索 ≤2 次、格式非法重生成 ≤2 次
MAX_RECOVER = 3
MAX_RE_RETRIEVE = 2
MAX_REGEN = 2


def trace_card(node: str, title: str, summary: str, payload: dict | None = None) -> dict:
    """节点即卡片：一张执行卡（SSE trace 事件与 state.trace 共用此形状）。"""
    return {"node": node, "title": title, "summary": summary, "payload": payload or {}}
