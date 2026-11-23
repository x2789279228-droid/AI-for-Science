"""模块七 R4：memory 端点单测（TestClient，全离线）。

覆盖三端点：GET /api/memory/timeline（ISO 8601 输出 + 区间/kind 过滤 + 返回集内边）、
GET /api/memory/hypotheses（FSM 状态 + 转移历史 + 合法下一态）、POST /api/memory/brief。
"""
from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from app.memory import store
from app.memory.timeparse import LOCAL_TZ

TZ = LOCAL_TZ


def _client() -> TestClient:
    from app.main import app
    return TestClient(app)


def test_timeline_iso_and_filters():
    c = _client()
    conv = "rt_tl"
    h = store.add_event("hypothesis", "我假设时间线页可以直接消费该端点", user_id="local",
                        conversation_id=conv, turn=1, thread_id=f"{conv}:1",
                        ts=datetime(2026, 11, 1, 10, 0, tzinfo=TZ).timestamp(), source="rule")
    f = store.add_event("finding", "时间线端点的联调结论", user_id="local",
                        conversation_id=conv, turn=2, thread_id=f"{conv}:2",
                        ts=datetime(2026, 11, 10, 10, 0, tzinfo=TZ).timestamp(), source="rule")
    store.upsert_edge(h["id"], f["id"], "same_topic")
    r = c.get(f"/api/memory/timeline?conversation_id={conv}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["meta"]["n_events"] == 2 and data["meta"]["n_edges"] == 1
    ev = data["events"][0]
    assert ev["ts_iso"].endswith("+08:00"), "时间必须输出 ISO 8601 带 +08:00 偏移"
    assert data["edges"][0]["relation"] == "same_topic"
    # kind 过滤
    r2 = c.get(f"/api/memory/timeline?conversation_id={conv}&kind=hypothesis")
    assert r2.json()["meta"]["n_events"] == 1
    assert r2.json()["meta"]["n_edges"] == 0, "边必须限定在返回事件集内"
    # ISO 区间过滤（11-05 之后只留 11-10 的事件）；+ 必须经 params 编码（裸拼会被读成空格）
    r3 = c.get("/api/memory/timeline",
               params={"conversation_id": conv, "start": "2026-11-05T00:00:00+08:00"})
    assert r3.json()["meta"]["n_events"] == 1
    assert r3.json()["events"][0]["id"] == f["id"]
    # 非法时间 → 400
    assert c.get("/api/memory/timeline?start=not-a-date").status_code == 400


def test_hypotheses_fsm_fields():
    c = _client()
    conv = "rt_h"
    h = store.add_event("hypothesis", "我假设端点测试假设会被正确序列化", user_id="local",
                        conversation_id=conv, turn=1, thread_id=f"{conv}:1", source="rule")
    store.transition(h["id"], "active", why="端点测试转移", verdict_source="evidence")
    r = c.get("/api/memory/hypotheses")
    assert r.status_code == 200
    item = next(x for x in r.json()["hypotheses"] if x["id"] == h["id"])
    assert item["status"] == "active"
    assert item["legal_next"] == ["refuted", "superseded", "supported"]
    assert len(item["transitions"]) == 1
    assert item["transitions"][0]["why"] == "端点测试转移"
    assert item["ts_iso"].endswith("+08:00")


def test_brief_endpoint():
    c = _client()
    conv = "rt_brief"
    store.add_event("hypothesis", "我假设简报端点返回 unverified 不冒充", user_id="local",
                    conversation_id=conv, turn=1, thread_id=f"{conv}:1", source="rule")
    r = c.post("/api/memory/brief", json={"conversation_id": conv})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["engine"] == "rule", "无 LLM 环境 engine 必须如实为 rule"
    assert data["meta"]["n_items"] >= 1
    item = next(i for i in data["items"] if "简报端点" in i["hypothesis"]["content"])
    assert item["verdict"] == "unverified" and item["verdict_source"] == ""
    assert item["hypothesis"]["thread_id"] == f"{conv}:1", "M# 回放字段必须随简报下发"


def test_get_event_404():
    c = _client()
    assert c.get("/api/memory/events/me_missing").status_code == 404
    ev = store.add_event("topic", "回读测试", user_id="local", conversation_id="rt_g")
    r = c.get(f"/api/memory/events/{ev['id']}")
    assert r.status_code == 200 and r.json()["ts_iso"].endswith("+08:00")


# ---------- R5：timeline expr 参数 / 简报缓存 / chat SSE memory_brief ----------

def test_timeline_expr_filter():
    """自然语言时间表达式直接作为筛选参数（前端筛选框的数据通道）。"""
    import time as _time
    c = _client()
    conv = "rt_expr"
    store.add_event("finding", "最近一周内的结论事件", user_id="local",
                    conversation_id=conv, turn=1, ts=_time.time() - 86400, source="rule")
    store.add_event("finding", "三个月前的旧结论事件", user_id="local",
                    conversation_id=conv, turn=2, ts=_time.time() - 90 * 86400, source="rule")
    r = c.get("/api/memory/timeline", params={"conversation_id": conv, "expr": "最近一周"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["meta"]["time_range"]["expr"] == "最近一周"
    assert data["meta"]["n_events"] == 1
    assert "最近一周内" in data["events"][0]["content"]
    assert c.get("/api/memory/timeline", params={"expr": "表达不清的时间"}).status_code == 400


def test_brief_cache_generation_invalidation():
    """代数戳缓存：同代数命中缓存（同对象）；事件增删后代数变化 → 重算。"""
    from app.memory import briefing
    conv = "rt_cache"
    h = store.add_event("hypothesis", "我假设缓存代数戳会正确失效", user_id="local",
                        conversation_id=conv, turn=1, thread_id=f"{conv}:1", source="rule")
    b1 = briefing.cached_brief(conversation_id=conv)
    assert b1.get("empty") is not True
    b2 = briefing.cached_brief(conversation_id=conv)
    assert b2 is b1, "同代数必须命中缓存（同一对象）"
    store.add_event("finding", "新事件使代数 +1", user_id="local",
                    conversation_id=conv, turn=2, source="rule")
    b3 = briefing.cached_brief(conversation_id=conv)
    assert b3 is not b1, "事件数变化后必须重算"
    assert store.get_event(h["id"]) is not None


def test_chat_sse_emits_memory_brief():
    """chat 开场 SSE：meta 之后直发 memory_brief；空简报 {empty:true}；use_memory=false 不发。"""
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app
    with db.tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO papers(id,title,file_path,file_hash,created_at,status,doc_type) "
            "VALUES('p_memchat','记忆链路测试论文','data/x.pdf','hash_memchat',?,'active','paper')",
            (db.now(),))
    c = TestClient(app)
    r = c.post("/api/chat", json={"question": "这篇论文讲了什么",
                                  "paper_ids": ["p_memchat"], "use_memory": True})
    assert r.status_code == 200
    text = r.text
    assert "event: meta" in text
    assert "event: memory_brief" in text, "use_memory=true 必须下发开场简报事件"
    assert '"empty": true' in text, "新会话无记忆 → 简报必须是 empty 态"
    assert "event: done" in text
    r2 = c.post("/api/chat", json={"question": "这篇论文讲了什么",
                                   "paper_ids": ["p_memchat"], "use_memory": False})
    assert "memory_brief" not in r2.text


# ---------- r8：PATCH append-only 修正 ----------

def test_patch_content_creates_correction_chain():
    """content 修正不原地改：新事件（source=manual, meta.corrected_from）+ 旧事件 superseded。"""
    c = _client()
    ev = store.add_event("hypothesis", "我假设原始内容待修正", user_id="local",
                         conversation_id="rt_patch", turn=1, thread_id="rt_patch:1",
                         importance=6, source="rule")
    r = c.patch(f"/api/memory/events/{ev['id']}",
                json={"content": "我假设修正后的准确内容", "note": "用户手动修正"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mode"] == "corrected"
    new_ev = data["new_event"]
    assert new_ev["source"] == "manual"
    assert new_ev["meta"]["corrected_from"] == ev["id"]
    assert new_ev["meta"]["correction_note"] == "用户手动修正"
    assert new_ev["content"] == "我假设修正后的准确内容"
    old = store.get_event(ev["id"])
    assert old["status"] == "superseded"
    assert old["meta"].get("superseded_by") == new_ev["id"]


def test_patch_terminal_event_reactivates():
    """终态事件被修正 → 新事件回到 active（修正后的事实需重新判定，如实可审计）。"""
    ev = store.add_event("hypothesis", "被推翻的旧结论", user_id="local",
                         conversation_id="rt_patch2", turn=1, source="rule")
    store.supersede_event(ev["id"], why="测试终态")
    c = _client()
    r = c.patch(f"/api/memory/events/{ev['id']}",
                json={"content": "修正后重新进入活跃集", "note": "推翻判定有误"})
    assert r.status_code == 200
    assert r.json()["new_event"]["status"] == "active", "终态修正 → 新事件回 active 重新判定"


def test_patch_status_transition_and_illegal():
    c = _client()
    h = store.add_event("hypothesis", "我假设状态修正走 FSM", user_id="local",
                        conversation_id="rt_patch3", turn=1, source="rule")
    r = c.patch(f"/api/memory/events/{h['id']}",
                json={"status": "active", "note": "手动激活"})
    assert r.status_code == 200 and r.json()["mode"] == "transition"
    assert r.json()["event"]["status"] == "active"
    assert r.json()["event"]["verdict_source"] == "manual"
    # 非法转移：active → proposed → 400
    r2 = c.patch(f"/api/memory/events/{h['id']}", json={"status": "supported"})
    assert r2.status_code == 200
    r3 = c.patch(f"/api/memory/events/{h['id']}", json={"status": "proposed"})
    assert r3.status_code == 400
    # 非 hypothesis 事件状态修正 → 400
    t = store.add_event("topic", "普通话题", user_id="local", conversation_id="rt_patch3")
    assert c.patch(f"/api/memory/events/{t['id']}", json={"status": "active"}).status_code == 400


def test_patch_validation():
    c = _client()
    assert c.patch("/api/memory/events/me_none",
                   json={"content": "x"}).status_code == 404
    ev = store.add_event("topic", "原内容", user_id="local", conversation_id="rt_pv")
    assert c.patch(f"/api/memory/events/{ev['id']}", json={}).status_code == 400
    same = c.patch(f"/api/memory/events/{ev['id']}", json={"content": "原内容"})
    assert same.status_code == 400


# ---------- r8：健康报告 memory_health 只读披露 ----------

def test_health_report_memory_block():
    c = _client()
    store.add_event("hypothesis", "我假设健康披露包含记忆块", user_id="local",
                    conversation_id="rt_hp", turn=1, source="rule")
    r = c.get("/api/health-report")
    assert r.status_code == 200, r.text
    mh = r.json().get("memory_health")
    assert mh is not None, "有记忆数据时 memory_health 必须下发"
    assert mh["events"] >= 1 and mh["hypotheses"] >= 1
    assert mh["healthy"] is True
    assert "不入 7 子分权重" in mh["note"]
