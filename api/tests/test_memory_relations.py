"""模块七 P1：graph LLM 建边升级单测（全离线——LLM 路经 monkeypatch，规则回落即被测对象）。

对应清单 §一 内部替换②b：LLM 判六类关系（kind=llm，memory_relations.yaml 版本化）；
同事件对上关系不同的旧边（含规则边）superseded 保留演化史；离线自动回落规则路。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.llm.client import LLMResult
from app.memory import graph, store
from app.memory.timeparse import LOCAL_TZ

TZ = LOCAL_TZ
T0 = datetime(2026, 11, 1, tzinfo=TZ).timestamp()
T1 = datetime(2026, 11, 10, tzinfo=TZ).timestamp()


def _pair(conv: str):
    """建一对 (旧假设, 新实验) 事件。每用例独立 user_id——conftest 是会话级共享库，
    同内容事件会互为建边候选污染 top_n（隔离是断言确定性的前提）。"""
    user = f"u_{conv}"
    old = store.add_event("hypothesis", "我假设稀疏注意力可以降低显存占用",
                          user_id=user, conversation_id=conv, turn=1,
                          thread_id=f"{conv}:1", ts=T0, source="rule")
    new = store.add_event("experiment_result", "稀疏注意力降低显存占用的对照实验完成",
                          user_id=user, conversation_id=conv, turn=2,
                          thread_id=f"{conv}:2", ts=T1, source="rule")
    return old, new, user


@pytest.fixture(autouse=True)
def _llm_off(monkeypatch):
    """默认 LLM 不可用（密闭环境口径）；LLM 路用例显式覆盖。"""
    monkeypatch.setattr(graph.llm, "available", lambda: False)


def test_offline_use_llm_falls_back_to_rule():
    _old, new, user = _pair("rel1")
    edges = graph.link_event(new["id"], user_id=user, use_llm=True)
    assert edges, "离线时 use_llm 必须回落规则路而非空手而归"
    assert all(e["kind"] == "rule" for e in edges)


def test_llm_relation_creates_kind_llm_edge(monkeypatch):
    old, new, user = _pair("rel2")
    monkeypatch.setattr(graph.llm, "available", lambda: True)
    monkeypatch.setattr(graph.llm, "chat", lambda *a, **k: LLMResult(
        {"text": '{"relation": "supports"}'}))
    edges = graph.link_event(new["id"], user_id=user, use_llm=True)
    sup = [e for e in edges if e["relation"] == "supports"]
    assert sup and sup[0]["kind"] == "llm"
    assert sup[0]["src"] == old["id"] and sup[0]["dst"] == new["id"], \
        "supports 沿时序先序：旧→新"


def test_llm_edge_supersedes_conflicting_rule_edge(monkeypatch):
    old, new, user = _pair("rel3")
    # 先有规则边：same_topic（旧→新）
    graph.link_event(new["id"], user_id=user, use_llm=False)
    assert store.list_edges(src=old["id"], dst=new["id"], relation="same_topic")
    # LLM 判定 refutes（语义方向：新→旧）→ 同对的 same_topic 规则边必须被 superseded
    monkeypatch.setattr(graph.llm, "available", lambda: True)
    monkeypatch.setattr(graph.llm, "chat", lambda *a, **k: LLMResult(
        {"text": '{"relation": "refutes"}'}))
    edges = graph.link_event(new["id"], user_id=user, use_llm=True)
    rf = [e for e in edges if e["relation"] == "refutes"]
    assert rf and rf[0]["kind"] == "llm" and rf[0]["src"] == new["id"]
    legacy = store.list_edges(src=old["id"], dst=new["id"], relation="same_topic",
                              status="superseded")
    assert legacy and legacy[0]["evidence"].get("superseded_by_relation") == "refutes", \
        "规则边 superseded 保留演化史（不删除）"
    assert not store.list_edges(src=old["id"], dst=new["id"], relation="same_topic",
                                status="active")


def test_llm_none_falls_back_to_rule(monkeypatch):
    _old, new, user = _pair("rel4")
    monkeypatch.setattr(graph.llm, "available", lambda: True)
    monkeypatch.setattr(graph.llm, "chat",
                        lambda *a, **k: LLMResult({"text": '{"relation": "none"}'}))
    edges = graph.link_event(new["id"], user_id=user, use_llm=True)
    assert all(e["kind"] == "rule" for e in edges), "none = 不可判 → 规则路兜底"


def test_llm_invalid_payload_falls_back(monkeypatch):
    _old, new, user = _pair("rel5")
    monkeypatch.setattr(graph.llm, "available", lambda: True)
    monkeypatch.setattr(graph.llm, "chat", lambda *a, **k: LLMResult({"text": "not json"}))
    edges = graph.link_event(new["id"], user_id=user, use_llm=True)
    assert all(e["kind"] == "rule" for e in edges)


def test_relations_prompt_versioned():
    from app.llm import prompts
    cat = {c["module"]: c for c in prompts.catalog()}
    assert cat["memory_relations"]["version"] == "1.0.0"
    p = prompts.get_prompt("memory_relations", "system_relations")
    assert p is not None and "refutes" in p.text
