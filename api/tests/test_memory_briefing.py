"""模块七 R4：研究简报单测（全离线——judge 路自动降级，规则/图谱边判定链本身即被测对象）。

对应清单 §三#7（verdict_source / unverified 不冒充）、§2.2（判定合法时推进 FSM，
非法转移跳过）、§2.12（简报 = extrapolation）。
"""
from __future__ import annotations

from datetime import datetime

from app.memory import briefing, graph, store
from app.memory.timeparse import LOCAL_TZ

TZ = LOCAL_TZ
T0 = datetime(2026, 11, 1, 10, 0, tzinfo=TZ).timestamp()
T1 = datetime(2026, 11, 10, 10, 0, tzinfo=TZ).timestamp()
U = "u_brf"


def _hyp(content: str, conv: str) -> dict:
    return store.add_event("hypothesis", content, user_id=U, conversation_id=conv,
                           turn=1, thread_id=f"{conv}:1", ts=T0, source="rule")


def _item(brief: dict, hyp_id: str) -> dict:
    matches = [i for i in brief["items"] if i["hypothesis"]["id"] == hyp_id]
    assert matches, f"简报必须包含假设 {hyp_id}：{[i['hypothesis']['id'] for i in brief['items']]}"
    return matches[0]


def test_refuted_chain_with_fsm_transition():
    hyp = _hyp("我假设稀疏注意力可以降低显存占用三成", "brf1")
    ev = store.add_event("experiment_result", "新实验推翻了稀疏注意力降低显存占用的假设",
                         user_id=U, conversation_id="brf1", turn=2, ts=T1, source="rule")
    graph.link_event(ev["id"], user_id=U)          # 规则建边：refutes（新→旧）
    brief = briefing.build_brief(user_id=U)
    item = _item(brief, hyp["id"])
    assert item["verdict"] == "refuted"
    assert item["verdict_source"] == "evidence", "图谱边判定的判定源必须如实标 evidence"
    assert brief["engine"] == "rule", "无 LLM 环境必须走规则路"
    assert item["evidence"], "判定必须附证据链"
    got = store.get_event(hyp["id"])
    assert got["status"] == "refuted", "判定合法时必须推进 FSM"
    tr = got["meta"]["transitions"]
    assert [(t["from"], t["to"]) for t in tr] == [("proposed", "active"), ("active", "refuted")]


def test_supported_chain():
    hyp = _hyp("我假设混合检索可以提升召回率两成", "brf2")
    ev = store.add_event("experiment_result", "实验证实混合检索对召回率的提升明显",
                         user_id=U, conversation_id="brf2", turn=2, ts=T1, source="rule")
    store.upsert_edge(ev["id"], hyp["id"], "supports")
    item = _item(briefing.build_brief(user_id=U), hyp["id"])
    assert item["verdict"] == "supported" and item["verdict_source"] == "evidence"
    assert store.get_event(hyp["id"])["status"] == "supported"


def test_neutral_with_unjudged_evidence():
    hyp = _hyp("我假设稀疏注意力可以降低显存占用三成", "brf3")
    store.add_event("finding", "我们讨论了稀疏注意力的实现细节", user_id=U,
                    conversation_id="brf3", turn=2, ts=T1, source="rule")
    item = _item(briefing.build_brief(user_id=U), hyp["id"])
    assert item["verdict"] == "neutral", "有相关证据但无判定边 → neutral，不冒充"
    assert item["verdict_source"] == "rule"
    assert store.get_event(hyp["id"])["status"] == "proposed", "neutral 不推进 FSM"


def test_unverified_no_fabrication():
    hyp = _hyp("我假设课程学习能加速收敛", "brf4")
    item = _item(briefing.build_brief(user_id=U), hyp["id"])
    assert item["verdict"] == "unverified", "无证据必须标 unverified，绝不冒充判定"
    assert item["verdict_source"] == ""
    assert item["evidence"] == []
    assert store.get_event(hyp["id"])["status"] == "proposed"


def test_illegal_transition_skipped_but_verdict_reported():
    hyp = _hyp("我假设注意力反序列化提升速度", "brf5")
    ev = store.add_event("experiment_result", "实验证实注意力反序列化确实提升速度",
                         user_id=U, conversation_id="brf5", turn=2, ts=T1, source="rule")
    store.upsert_edge(ev["id"], hyp["id"], "supports")
    briefing.build_brief(user_id=U)                 # supported：proposed→active→supported
    ev2 = store.add_event("experiment_result", "复现实验推翻了注意力反序列化的速度结论",
                          user_id=U, conversation_id="brf5", turn=3, ts=T1 + 86400,
                          source="rule")
    store.upsert_edge(ev2["id"], hyp["id"], "refutes")
    item = _item(briefing.build_brief(user_id=U), hyp["id"])
    assert item["verdict"] == "refuted", "判定如实上报"
    assert item["transitions_applied"] == [], "supported→refuted 非法 → 转移跳过"
    assert store.get_event(hyp["id"])["status"] == "supported", "FSM 合法转移表未被破坏"


def test_tracked_items_as_evidence():
    hyp = _hyp("我假设稀疏注意力可以降低显存占用三成", "brf6")
    from app import db
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO tracked_items(id,sub_id,arxiv_id,title,status,added_at) "
                  "VALUES('ti_brf','sub_brf','2601.12345',?,'new',?)",
                  ("稀疏注意力降低显存占用的新方法", T1))
    item = _item(briefing.build_brief(user_id=U), hyp["id"])
    sources = {e["source"] for e in item["evidence"]}
    assert "tracked" in sources, "tracked_items 新条目必须进入证据链"
    assert item["verdict"] == "neutral", "追踪条目仅是相关证据，无判定边 → neutral"


def test_past_hypotheses_listed():
    _hyp("我假设已终结的旧方向", "brf7")
    h = store.get_event(store.list_events(kind="hypothesis", conversation_id="brf7")[0]["id"])
    store.supersede_event(h["id"], why="测试终态")
    brief = briefing.build_brief(user_id=U)
    assert any(p["id"] == h["id"] for p in brief["past_hypotheses"]), \
        "终态假设必须进信念演化史列表"
    assert all(i["hypothesis"]["id"] != h["id"] for i in brief["items"]), \
        "终态假设不再进入 active 简报"
