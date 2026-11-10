"""arXiv 追踪测试（模块四 P0-3，密闭）：订阅 CRUD / 双通道拉取 mock / 去重入库 /
水位线推进 / 库内已知标记 / worker 启停 / 端点。网络全 mock。"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

os.environ.setdefault("SCHOLAR_DATA_DIR",
                      str(Path(__file__).parent / f"_trk_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_API_KEY"] = ""
os.environ["MINERU_ENABLED"] = "off"
os.environ["EMBEDDING_BACKEND"] = "off"

from app import db
from app.agents import tracker

_ATOM_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>http://arxiv.org/abs/2401.00001v1</id><title>Graph Neural Tracking A</title>
    <author><name>Alice Chen</name></author><published>2026-11-08T00:00:00Z</published></entry>
  <entry><id>http://arxiv.org/abs/2401.00002v2</id><title>Unrelated Chemistry Paper</title>
    <author><name>Bob Li</name></author><published>2026-11-07T00:00:00Z</published></entry>
  <entry><id>http://arxiv.org/abs/2401.00003v1</id><title>Tracking B with keyword hit</title>
    <author><name>Carol Wu</name></author><published>2026-11-06T00:00:00Z</published></entry>
</feed>"""


def _sub(name: str, qtype: str, value: str, last_run: float = 0.0) -> dict:
    sid = db.new_id("ts_")
    with db.tx() as c:
        c.execute("INSERT INTO tracking_subscriptions(id,name,query_type,value,created_at,"
                  "last_run) VALUES(?,?,?,?,?,?)", (sid, name, qtype, value, time.time(), last_run))
    return {"id": sid, "name": name, "query_type": qtype, "value": value, "last_run": last_run}


def _mock_atom(monkeypatch, xml: str = _ATOM_XML):
    class _R:
        status_code = 200
        text = xml

        def raise_for_status(self):
            pass

    monkeypatch.setattr(tracker.httpx, "get", lambda url, **kw: _R())


def test_refresh_keyword_filter_dedup_watermark(monkeypatch):
    db.init_db()
    with db.tx() as c:
        c.execute("DELETE FROM tracked_items")
        c.execute("DELETE FROM tracking_subscriptions")
    # last_run 显式固定回看窗口起点（fixture 日期硬编码 11-06~08；默认 now-3d 的
    # 相对窗口会随日历漂移把 11-06 滤掉——2026-11-10 实际翻车）
    import calendar
    sub = _sub("图神经网络", "keyword", "tracking",
               last_run=calendar.timegm((2026, 11, 1, 0, 0, 0)))
    _mock_atom(monkeypatch)
    r1 = tracker.refresh_subscription(sub)
    assert r1["channel"] == "atom" and r1["added"] == 2, "关键词过滤只留标题命中 tracking 的两条"
    items = tracker.list_items(sub_id=sub["id"])
    assert {i["arxiv_id"] for i in items} == {"2401.00001", "2401.00003"}

    # 二次刷新：去重不重复入库；水位线已推进（last_run=now → from=昨日 → 日期过滤后仍至少命中一条）
    _mock_atom(monkeypatch)
    r2 = tracker.refresh_subscription(sub | {"last_run": time.time()})
    assert r2["added"] == 0 and r2["deduped"] >= 1, "UNIQUE(sub_id, arxiv_id) 去重"
    row = db.query("SELECT last_run FROM tracking_subscriptions WHERE id=?", (sub["id"],))[0]
    assert row["last_run"] > 0, "水位线必须推进（重启续跑锚点）"


def test_refresh_marks_known_library_items(monkeypatch):
    # Round6：Sickle 打桩隔离——真实 OAI 顶层 cs 偶尔秒回（网络态相关），
    # 抢走 Atom mock 路径致 2401.00001 不入库（偶发红根因）
    monkeypatch.setattr(tracker, "_fetch_sickle", lambda *a, **k: None)
    sub = _sub("cat-cl", "category", "cs.CL")
    with db.tx() as c:
        c.execute("DELETE FROM tracked_items WHERE sub_id=?", (sub["id"],))
        c.execute("INSERT OR REPLACE INTO papers(id,title,arxiv_id,file_path,file_hash,"
                  "created_at) VALUES('p_known','Known Paper','2401.00001','','h_known',0)")
    _mock_atom(monkeypatch)
    tracker.refresh_subscription(sub)
    rows = {r["arxiv_id"]: r["status"] for r in db.query(
        "SELECT arxiv_id,status FROM tracked_items WHERE sub_id=?", (sub["id"],))}
    assert rows.get("2401.00001") == "known", "库内已有论文标 known，不重复入库"
    assert rows.get("2401.00002") == "new"
    with db.tx() as c:
        c.execute("DELETE FROM papers WHERE id='p_known'")


def test_sickle_channel_preferred_for_category(monkeypatch):
    sub = _sub("sickle-cat", "category", "cs.LG")
    calls = {"sickle": 0, "atom": 0}

    def fake_sickle(set_spec, from_date, limit):
        calls["sickle"] += 1
        return [{"arxiv_id": "2402.00001", "title": "Sickle Fetched", "authors": ["X"],
                 "published": "2026-11-08"}]

    monkeypatch.setattr(tracker, "_fetch_sickle", fake_sickle)

    def fail_atom(*a, **kw):
        calls["atom"] += 1
        raise AssertionError("Sickle 命中时不应走 Atom")

    monkeypatch.setattr(tracker, "_fetch_atom", fail_atom)
    r = tracker.refresh_subscription(sub)
    assert calls["sickle"] == 1 and r["channel"] == "sickle"


def test_worker_start_stop():
    assert tracker.start_tracking_worker() is True, "首次启动成功"
    assert tracker.start_tracking_worker() is False, "幂等：重复启动不起新线程"
    tracker.stop_tracking_worker()


def test_tracking_endpoints(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)
    r = client.post("/api/tracking/subscriptions",
                    json={"name": "大模型", "query_type": "keyword", "value": "llm"})
    assert r.status_code == 200
    sid = r.json()["subscription"]["id"]

    _mock_atom(monkeypatch)
    r2 = client.post("/api/tracking/refresh")
    assert r2.status_code == 200 and any(x["sub_id"] == sid for x in r2.json()["results"])

    r3 = client.get("/api/tracking/items", params={"sub_id": sid})
    assert r3.status_code == 200 and isinstance(r3.json()["items"], list)

    item_id = r3.json()["items"][0]["id"] if r3.json()["items"] else None
    if item_id:
        assert client.post(f"/api/tracking/items/{item_id}/dismiss").status_code == 200

    assert client.delete(f"/api/tracking/subscriptions/{sid}").status_code == 200
    assert client.post("/api/tracking/subscriptions",
                       json={"name": "bad", "query_type": "doi", "value": "x"}).status_code == 400
