"""模块七 r9：记忆问答（memory_qa）单测——离线确定性兜底 / LLM 路 / 分流门。

背景：A/B 三轮实测发现文献问答角色框把记忆问题错答成分级拒答 → 架构级分流：
用户历史研究问题由记忆时间线直接作答（[M#] 引用，不走论文证据环）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.llm.client import LLMResult
from app.memory import memory_qa, retrieve, store
from app.memory.timeparse import LOCAL_TZ

TZ = LOCAL_TZ
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=TZ).timestamp()


@pytest.fixture(autouse=True)
def _llm_off(monkeypatch):
    monkeypatch.setattr(memory_qa.llm, "available", lambda: False)


def _anchors():
    ref = store.add_event("experiment_result",
                          "复现实验推翻了稀疏注意力降低 40% 显存的假设：实测仅 12%",
                          user_id="u_mqa", conversation_id="mqa", turn=2, ts=NOW - 6 * 86400,
                          source="rule")
    return {"M1": {"memory_id": ref["id"], "kind": "experiment_result", "status": "active",
                   "conversation_id": "mqa", "turn": 2, "thread_id": "mqa:2",
                   "content": ref["content"], "ts": ref["ts"]}}


def test_offline_fallback_lists_timeline_with_citations():
    anchors = _anchors()
    out = memory_qa.answer_from_memory("显存优化的假设现状如何？", anchors, use_llm=False)
    assert out["mode"] == "extractive"
    assert "[M1]" in out["answer"]
    assert "12%" in out["answer"], "离线兜底必须引用时间线原文（可溯源）"
    assert out["m_citations"] >= 1


def test_offline_fallback_marks_hypothesis_status():
    h = store.add_event("hypothesis", "我假设待标注状态的假设", user_id="u_mqa2",
                        conversation_id="mqa2", turn=1, source="rule")
    store.transition(h["id"], "active", why="测试前置")
    store.transition(h["id"], "refuted", why="测试")
    anchors = {"M1": {"memory_id": h["id"], "kind": "hypothesis", "status": "refuted",
                      "conversation_id": "mqa2", "turn": 1, "thread_id": "mqa2:1",
                      "content": h["content"], "ts": NOW}}
    out = memory_qa.answer_from_memory("问题", anchors, use_llm=False)
    assert "已推翻" in out["answer"], "hypothesis 记忆必须带 FSM 状态标注"


def test_llm_path_returns_synthesized_answer(monkeypatch):
    monkeypatch.setattr(memory_qa.llm, "available", lambda: True)
    monkeypatch.setattr(memory_qa.llm, "chat", lambda *a, **k: LLMResult(
        {"text": "你的显存优化假设已被复现实验推翻，实测仅 12% [M1]。"}))
    out = memory_qa.answer_from_memory("问题", _anchors(), use_llm=True)
    assert out["mode"] == "llm" and out["m_citations"] == 1


def test_llm_output_without_citation_falls_back(monkeypatch):
    monkeypatch.setattr(memory_qa.llm, "available", lambda: True)
    monkeypatch.setattr(memory_qa.llm, "chat",
                        lambda *a, **k: LLMResult({"text": "泛泛而谈没有引用"}))
    out = memory_qa.answer_from_memory("问题", _anchors(), use_llm=True)
    assert out["mode"] == "extractive", "缺 [M#] 引用的 LLM 输出必须回落确定性兜底"


def test_gate_threshold(monkeypatch):
    items = [{"scores": {"hops": 0, "relevance_raw": 0.3}},
             {"scores": {"hops": 0, "relevance_raw": 0.05}}]
    assert memory_qa.gate_from_ranked(items)["trigger"] is True
    weak = [{"scores": {"hops": 0, "relevance_raw": 0.05}}]
    assert memory_qa.gate_from_ranked(weak)["trigger"] is False


def test_gate_via_build_memory_block():
    store.add_event("hypothesis", "我假设稀疏注意力可以降低显存占用", user_id="u_gate",
                    conversation_id="gt", turn=1, ts=NOW - 86400, source="rule")
    _, _, gate = retrieve.build_memory_block("稀疏注意力 显存", doc_mode="paper",
                                             user_id="u_gate", now_ts=NOW)
    assert gate["trigger"] is True
