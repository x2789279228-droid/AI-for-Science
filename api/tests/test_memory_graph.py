"""模块七 R3：时间图谱规则版建边 + 时间线路径单测（全离线）。

对应清单 §三#5（THEANINE Phase I 规则版 / Phase II 线性时间线）、§2.5（边 UPSERT/方向）、
§一 内部替换②a（P0 规则版，evidence.engine 如实标注）。
"""
from __future__ import annotations

from datetime import datetime

from app.memory import graph, store
from app.memory.timeparse import LOCAL_TZ

TZ = LOCAL_TZ
T0 = datetime(2026, 9, 1, 10, 0, tzinfo=TZ).timestamp()
T1 = datetime(2026, 10, 1, 10, 0, tzinfo=TZ).timestamp()
T2 = datetime(2026, 11, 1, 10, 0, tzinfo=TZ).timestamp()


def _add(kind, content, ts, user="u_graph", **kw):
    return store.add_event(kind, content, user_id=user, ts=ts, source="rule", **kw)


def test_same_topic_rule_older_to_newer():
    old = _add("finding", "稀疏注意力机制可以显著降低显存占用", T0)
    new = _add("finding", "稀疏注意力机制在长文本下显著降低显存", T1)
    edges = graph.link_event(new["id"], user_id="u_graph")
    st = [e for e in edges if e["relation"] == "same_topic"]
    assert st, "高词面重叠必须建 same_topic"
    assert st[0]["src"] == old["id"] and st[0]["dst"] == new["id"], "same_topic 时序先序：旧→新"
    assert st[0]["kind"] == "rule"
    ev_list = st[0]["evidence"]["evidence"]
    assert ev_list and ev_list[-1]["engine"] == "lexical", "无向量环境必须如实标注词面引擎"
    assert ev_list[-1]["sim"] >= graph.SAME_TOPIC_THRESHOLD


def test_refutes_rule_new_to_old():
    hyp = _add("hypothesis", "我假设注意力反序列化会提升训练速度", T0)
    ev = _add("experiment_result", "新实验推翻了注意力反序列化的速度假设", T1)
    edges = graph.link_event(ev["id"], user_id="u_graph")
    rf = [e for e in edges if e["relation"] == "refutes"]
    assert rf, "反驳标记必须建 refutes"
    assert rf[0]["src"] == ev["id"] and rf[0]["dst"] == hyp["id"], "refutes 语义方向：新→旧"


def test_supersedes_rule_new_to_old():
    hyp = _add("hypothesis", "假设方案甲是最优检索结构", T0)
    new_hyp = _add("hypothesis", "修正假设：方案乙取代原假设成为最优检索结构", T1)
    edges = graph.link_event(new_hyp["id"], user_id="u_graph")
    sup = [e for e in edges if e["relation"] == "supersedes"]
    assert sup and sup[0]["src"] == new_hyp["id"] and sup[0]["dst"] == hyp["id"]


def test_unrelated_no_edge():
    _add("topic", "用户喜欢手冲咖啡", T0)
    new = _add("topic", "讨论了会议室预订的审批流程", T1)
    assert graph.link_event(new["id"], user_id="u_graph") == []


def test_link_event_validates_owner():
    ev = _add("topic", "别的用户的事件", T0, user="u_other")
    assert graph.link_event(ev["id"], user_id="u_graph") == []


def test_build_for_conversation_idempotent():
    store.add_event("hypothesis", "稀疏注意力机制可降低显存占用", user_id="u_g2",
                    conversation_id="conv_g", ts=T0, source="rule")
    store.add_event("experiment_result", "稀疏注意力机制实验确认降低显存占用",
                    user_id="u_g2", conversation_id="conv_g", ts=T1, source="rule")
    first = graph.build_for_conversation("conv_g", user_id="u_g2")
    assert first and any(e["relation"] == "same_topic" for e in first), "会话补建边必须产出"
    second = graph.build_for_conversation("conv_g", user_id="u_g2")
    same_pairs = {(e["src"], e["dst"], e["relation"]) for e in first} & \
                 {(e["src"], e["dst"], e["relation"]) for e in second}
    assert same_pairs, "重复构建应命中 UPSERT 确认而非新边"
    for src, dst, rel in same_pairs:
        row = store.list_edges(src=src, dst=dst, relation=rel)[0]
        assert row["confirm_count"] >= 2


def test_timeline_path_linear_projection():
    e1 = _add("decision", "采用混合检索架构", T0, user="u_tl")
    e2 = _add("experiment_result", "混合检索实验证实召回提升", T1, user="u_tl")
    e3 = _add("finding", "最终确认召回提升的结论", T2, user="u_tl")
    store.upsert_edge(e1["id"], e2["id"], "supports")
    store.upsert_edge(e2["id"], e3["id"], "cause")
    out = graph.timeline_path([e3["id"]])
    assert [e["id"] for e in out["events"]] == [e1["id"], e2["id"], e3["id"]], "按 ts 线性化"
    assert [(l["src"], l["dst"], l["relation"]) for l in out["links"]] == [
        (e1["id"], e2["id"], "supports"), (e2["id"], e3["id"], "cause")]


def test_timeline_path_undirected_traversal():
    """边方向不影响连通：反向边（新→旧 refutes）也能把旧事件拉进路径。"""
    e1 = _add("hypothesis", "假设直连结构最优", T0, user="u_tl2")
    e2 = _add("experiment_result", "新证据推翻直连结构最优的假设", T1, user="u_tl2")
    store.upsert_edge(e2["id"], e1["id"], "refutes")
    out = graph.timeline_path([e2["id"]])
    assert {e["id"] for e in out["events"]} == {e1["id"], e2["id"]}
    assert out["links"][0]["relation"] == "refutes"
