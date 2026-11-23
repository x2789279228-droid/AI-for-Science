"""模块七 P1：memory_save 受控自写工具单测（全离线）。

对应清单 §三#13 / 已确认决策 #1：MemGPT self-editing 落地——白名单管控、source=agent、
只写事件（消解/建边交 worker）、与轮末抽取同轮去重、普通模式不禁用。
"""
from __future__ import annotations

import pytest

from app.agents import tools
from app.agents.state import GENERAL_DISABLED_TOOLS
from app.memory import store
from app.memory.extract import extract_turn, turn_ctx


@pytest.fixture(autouse=True)
def _turn_context():
    """置轮次上下文（runner 在图执行线程做的事）。"""
    turn_ctx.set(("conv_save", 3, "conv_save:3"))
    yield
    turn_ctx.set(("", 0, ""))


def test_tool_registered_and_whitelisted():
    spec = tools.TOOLS["memory_save"]
    assert {"reader", "compare", "writer", "graph"} <= spec.allowed_agents
    tools.assert_allowed("reader", "memory_save")            # 不抛
    with pytest.raises(tools.ToolDenied):
        tools.assert_allowed("viz", "memory_save")


def test_general_mode_not_disabled():
    """写记忆不是证据源——双模式下普通文档同样可用（清单 §三#13）。"""
    tools.set_doc_mode("general")
    try:
        tools.assert_allowed("reader", "memory_save")        # 不抛
        assert "memory_save" not in GENERAL_DISABLED_TOOLS
    finally:
        tools.set_doc_mode("paper")


def test_call_tool_writes_agent_event():
    out = tools.call_tool("reader", "memory_save", None,
                          content="我假设受控写入链路可用", kind="hypothesis",
                          subject="受控写入", importance=8)
    assert out["ok"] is True, out
    ev = store.get_event(out["data"]["memory_id"])
    assert ev["source"] == "agent", "agent 自写必须如实标注 source=agent"
    assert ev["conversation_id"] == "conv_save" and ev["turn"] == 3
    assert ev["thread_id"] == "conv_save:3"
    assert ev["kind"] == "hypothesis" and ev["status"] == "proposed"


def test_call_tool_invalid_kind_fails_gracefully():
    out = tools.call_tool("reader", "memory_save", None, content="x", kind="bogus")
    assert out["ok"] is False, "业务异常必须转 failure 而非上抛"
    assert "ValueError" in out.get("error", "") or out.get("failure")


def test_dedup_with_turn_extraction():
    """agent 先写同内容 → 轮末抽取跳过（清单 §三#13 去重规则）。"""
    out = tools.call_tool("reader", "memory_save", None,
                          content="我假设去重规则生效", kind="hypothesis")
    assert out["ok"]
    res = extract_turn("为什么", "我假设去重规则生效", conversation_id="conv_save", turn=3)
    assert res["created"] == [], "同轮同内容不得重复入库"
