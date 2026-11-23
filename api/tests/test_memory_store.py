"""模块七 R1：memory store 单测——三表 CRUD / 假设 FSM 合法转移表 / 边 UPSERT 与翻转（全离线）。

对应改造清单 §2.2（FSM 触发与审计）、§2.3（strength 初值=importance/10）、
§2.5（UNIQUE+UPSERT、关系翻转保留演化史）、§2.6（supersede 幂等）。
"""
from __future__ import annotations

import pytest

from app.memory import store
from app.memory.store import FSMError

# ---------- 事件 CRUD ----------

def test_add_and_get_roundtrip():
    ev = store.add_event("hypothesis", "注意力稀疏化能降低 30% 显存",
                         conversation_id="c1", turn=3, subject="稀疏注意力",
                         entities=["稀疏注意力"], importance=6, source="llm",
                         meta={"e_refs": ["E2"]})
    got = store.get_event(ev["id"])
    assert got["kind"] == "hypothesis"
    assert got["status"] == "proposed", "假设初始态必须是 proposed（FSM 起点）"
    assert got["strength"] == 0.6, "strength 初值 = importance/10（清单 §2.3）"
    assert got["entities"] == ["稀疏注意力"], "entities_json 须反序列化为列表"
    assert got["meta"]["e_refs"] == ["E2"], "meta_json 须反序列化"
    assert got["conversation_id"] == "c1" and got["turn"] == 3
    assert got["layer"] == "LML" and got["access_count"] == 0


def test_add_event_validates_inputs():
    with pytest.raises(ValueError, match="kind"):
        store.add_event("not_a_kind", "x")
    with pytest.raises(ValueError, match="source"):
        store.add_event("finding", "x", source="gpt")
    with pytest.raises(ValueError, match="importance"):
        store.add_event("finding", "x", importance=0)
    with pytest.raises(ValueError, match="importance"):
        store.add_event("finding", "x", importance=11)


def test_default_status_by_kind():
    assert store.add_event("finding", "实验 A 优于基线")["status"] == "active"
    assert store.add_event("topic", "讨论了评测协议")["status"] == "active"
    assert store.add_event("hypothesis", "假设 H")["status"] == "proposed"


def test_get_missing_returns_none():
    assert store.get_event("me_missing") is None


# ---------- 假设 FSM（清单 §2.2） ----------

def test_fsm_legal_full_path():
    ev = store.add_event("hypothesis", "假设 H1")
    e = store.transition(ev["id"], "active", why="出现首条同主题实验记忆",
                         verdict_source="evidence")
    e = store.transition(e["id"], "refuted", why="矛盾实验结果 + 新文献",
                         verdict_source="judge")
    e = store.transition(e["id"], "superseded", why="提出修正假设 H2")
    tr = e["meta"]["transitions"]
    assert [(t["from"], t["to"]) for t in tr] == [
        ("proposed", "active"), ("active", "refuted"), ("refuted", "superseded")]
    assert tr[1]["verdict_source"] == "judge" and "矛盾" in tr[1]["why"]
    assert e["status"] == "superseded"


def test_fsm_illegal_transitions_rejected():
    ev = store.add_event("hypothesis", "假设 H2")
    # proposed→supported 跳过 active：拒绝
    with pytest.raises(FSMError, match="proposed→supported"):
        store.transition(ev["id"], "supported")
    # refuted 是可再取代的中间态，但不能再回到 active
    store.transition(ev["id"], "active")
    store.transition(ev["id"], "refuted")
    with pytest.raises(FSMError, match="refuted→active"):
        store.transition(ev["id"], "active")
    # superseded 为终态
    store.transition(ev["id"], "superseded")
    with pytest.raises(FSMError, match="superseded→"):
        store.transition(ev["id"], "active")
    assert store.get_event(ev["id"])["status"] == "superseded"


def test_fsm_only_for_hypothesis():
    ev = store.add_event("finding", "普通发现不走状态机")
    with pytest.raises(FSMError, match="不参与状态机"):
        store.transition(ev["id"], "superseded")


def test_transition_missing_event():
    with pytest.raises(KeyError):
        store.transition("me_none", "active")


def test_supersede_idempotent_with_audit_meta():
    new = store.add_event("hypothesis", "修正假设 H2")
    old = store.add_event("hypothesis", "原始假设 H1")
    store.supersede_event(old["id"], by_id=new["id"], why="同主题修正")
    got = store.get_event(old["id"])
    assert got["status"] == "superseded"
    assert got["meta"]["superseded_by"] == new["id"]
    # 幂等：再次 supersede 不追加转移记录
    again = store.supersede_event(old["id"], by_id=new["id"])
    assert len(again["meta"]["transitions"]) == 1
    assert again["meta"] == got["meta"]


# ---------- 检索辅助 ----------

def test_touch_access_counters():
    ev = store.add_event("preference", "用户偏好中文回答")
    store.touch_access(ev["id"], at=1000.0)
    store.touch_access(ev["id"], at=2000.0)
    got = store.get_event(ev["id"])
    assert got["access_count"] == 2 and got["last_access"] == 2000.0


def test_list_events_filters():
    """注意：conftest 是会话级共享库，绝对计数会被其它用例污染——全部按唯一会话域断言。"""
    store.add_event("finding", "f1", conversation_id="ca", ts=100.0)
    store.add_event("hypothesis", "h1", conversation_id="ca", ts=200.0)
    store.add_event("finding", "f2", conversation_id="cb", ts=300.0)
    assert {e["content"] for e in store.list_events(kind="finding", conversation_id="ca")} == {"f1"}
    assert {e["content"] for e in store.list_events(conversation_id="ca")} == {"f1", "h1"}
    assert [e["content"] for e in
            store.list_events(conversation_id="ca", t_start=150, t_end=250)] == ["h1"]
    assert all(e["status"] == "proposed"
               for e in store.list_events(status="proposed", conversation_id="ca"))
    assert {e["content"] for e in store.list_events(conversation_id="cb")} == {"f2"}


# ---------- 实体消解（规则版） ----------

def test_resolve_entity_case_and_alias():
    a = store.resolve_entity("Transformer")
    b = store.resolve_entity("transformer")          # 大小写归一命中
    assert a["id"] == b["id"]
    # 追加别名后按别名命中
    import json as _json

    from app.db import tx
    with tx() as c:
        c.execute("UPDATE entities SET aliases_json=? WHERE id=?",
                  (_json.dumps(["transformer", "注意力架构"]), a["id"]))
    c2 = store.resolve_entity("注意力架构")
    assert c2["id"] == a["id"]
    d = store.resolve_entity("DiffLens")
    assert d["id"] != a["id"], "不同实体必须新建"


# ---------- 时间因果边（清单 §2.5） ----------

def test_edge_upsert_confirm_keeps_earliest_validity():
    a = store.add_event("hypothesis", "H1")
    b = store.add_event("experiment_result", "支持 H1 的实验")
    e1 = store.upsert_edge(a["id"], b["id"], "supports", t_valid_from=100.0)
    e2 = store.upsert_edge(a["id"], b["id"], "supports", evidence={"turn": 5},
                           t_valid_from=999.0)
    assert e1["id"] == e2["id"], "同三元组 UPSERT 不新增行"
    assert e2["confirm_count"] == 2
    assert e2["t_valid_from"] == 100.0, "保留最早 t_valid_from"
    assert e2["evidence"]["evidence"] == [{"turn": 5}]


def test_edge_conflict_flip_preserves_history():
    a = store.add_event("hypothesis", "H3")
    ev = store.add_event("experiment_result", "先支持后反驳的证据")
    s = store.upsert_edge(a["id"], ev["id"], "supports")
    r = store.upsert_edge(a["id"], ev["id"], "refutes")
    assert r["status"] == "active"
    sup = store.list_edges(src=a["id"], relation="supports", status="superseded")
    assert len(sup) == 1 and sup[0]["id"] == s["id"]
    assert sup[0]["evidence"]["superseded_by_relation"] == "refutes"
    # 翻转回来：supports 复活（新有效周期），refutes 被压成 superseded
    s2 = store.upsert_edge(a["id"], ev["id"], "supports")
    assert s2["id"] == s["id"] and s2["status"] == "active"
    assert s2["confirm_count"] == 2
    assert store.list_edges(src=a["id"], relation="refutes", status="superseded")
    assert not store.list_edges(src=a["id"], relation="refutes", status="active")


def test_edge_validates_relation():
    a = store.add_event("hypothesis", "H4")
    with pytest.raises(ValueError, match="relation"):
        store.upsert_edge(a["id"], a["id"], "likes")


def test_edges_filter_by_status():
    a = store.add_event("hypothesis", "H5")
    b = store.add_event("topic", "T1")
    store.upsert_edge(a["id"], b["id"], "same_topic")
    store.upsert_edge(a["id"], b["id"], "supersedes")
    assert len(store.list_edges(src=a["id"])) == 2
    assert len(store.list_edges(src=a["id"], relation="same_topic")) == 1
