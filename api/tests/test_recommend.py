"""推荐系统测试（模块四 P0-4，密闭）：画像、打分分量、理由、赞踩重加权、多用户隔离。"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SCHOLAR_DATA_DIR",
                      str(Path(__file__).parent / f"_rec_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_API_KEY"] = ""
os.environ["MINERU_ENABLED"] = "off"
os.environ["EMBEDDING_BACKEND"] = "off"

from app import db
from app.agents import recommend


@pytest.fixture(scope="module", autouse=True)
def _setup():
    db.init_db()
    with db.tx() as c:
        c.execute("DELETE FROM papers")
        c.execute("DELETE FROM tracked_items")
        c.execute("DELETE FROM tracking_subscriptions")
        c.execute("DELETE FROM recommendations")
        c.execute("DELETE FROM recommend_feedback")
        c.execute("DELETE FROM user_profiles")
        # 库内画像基础：两篇 transformer 相关
        for pid, title in (("p_lib1", "Attention Is All You Need"),
                           ("p_lib2", "BERT: Pre-training of Transformers")):
            c.execute("INSERT INTO papers(id,title,status,file_path,file_hash,created_at)"
                      " VALUES(?,?,?,?,?,?)", (pid, title, "active", "", f"h_{pid}", 0.0))
        # 追踪候选：一篇强相关带引用数、一篇无关
        sub = "ts_test"
        c.execute("INSERT INTO tracking_subscriptions(id,name,query_type,value,created_at)"
                  " VALUES(?,?,?,?,?)", (sub, "s", "keyword", "transformer", 0.0))
        now = time.time()
        c.execute("INSERT INTO tracked_items(id,sub_id,arxiv_id,title,published,authors,"
                  "status,added_at) VALUES(?,?,?,?,?,?,?,?)",
                  ("ti1", sub, "2401.00001", "Efficient Transformer Attention Scaling",
                   "2026-11-01", "[]", "new", now))
        c.execute("INSERT INTO tracked_items(id,sub_id,arxiv_id,title,published,authors,"
                  "status,added_at) VALUES(?,?,?,?,?,?,?,?)",
                  ("ti2", sub, "2401.00002", "Concrete Bridge Fracture Analysis",
                   "2026-10-01", "[]", "new", now))
    yield


def test_profile_keywords_from_library():
    kw = recommend.profile_keywords("anonymous")
    assert kw.get("transformer", 0) >= 1 and kw.get("attention", 0) >= 1
    assert "the" not in kw


def test_generate_scores_and_reason_order(monkeypatch):
    monkeypatch.setattr(recommend, "_github_repro", None, raising=False)
    monkeypatch.setattr("app.agents.tools._github_repro",
                        lambda title: {"ok": True, "repos": [
                            {"full_name": "x/y", "stars": 1200, "repro_score": 0.8}]}
                        if "Transformer" in title else {"ok": True, "repos": []})
    out = recommend.generate("u1", 5, with_github=True)
    recs = out["recommendations"]
    assert len(recs) == 2
    assert recs[0]["title"].startswith("Efficient Transformer"), "相关候选应排第一"
    top = recs[0]
    assert top["scores"]["local"] > 0.1 and top["scores"]["github"] == 0.8
    assert "Efficient Transformer" in top["reason"] or "命中" in top["reason"]
    assert "x/y" in top["reason"], "推荐理由含 GitHub 仓库"
    assert recs[1]["title"].startswith("Concrete"), "无关候选排后（local≈0）"
    assert recs[1]["scores"]["github"] == 0.0


def test_feedback_reweights_profile_and_persists():
    rows = db.query("SELECT id FROM recommendations WHERE user_id='u1' ORDER BY created_at")
    rid = rows[0]["id"]
    r = recommend.record_feedback(rid, "u1", 1)
    assert r["ok"]
    kw = recommend.profile_keywords("u1")
    assert kw.get("attention", 0) >= recommend.profile_keywords("u_no_fb").get("attention", 0) + 2
    votes = db.query("SELECT SUM(vote) v FROM recommend_feedback WHERE rec_id=?", (rid,))
    assert votes[0]["v"] == 1, "反馈持久化"
    recommend.record_feedback(rid, "u1", -1)
    votes2 = db.query("SELECT SUM(vote) v FROM recommend_feedback WHERE rec_id=?", (rid,))
    assert votes2[0]["v"] == 0


def test_multi_user_isolation():
    recommend.generate("user_a", 5, with_github=False)
    recommend.generate("user_b", 5, with_github=False)
    a = {r["title"] for r in recommend.list_for("user_a")}
    b = {r["title"] for r in recommend.list_for("user_b")}
    assert a and a == b, "内容相同（同一匿名冷启动画像）但行独立"
    assert db.query("SELECT COUNT(*) c FROM recommendations WHERE user_id='user_a'")[0]["c"] == 2
    # 反馈只影响自己的画像
    rid = db.query("SELECT id FROM recommendations WHERE user_id='user_a'")[0]["id"]
    recommend.record_feedback(rid, "user_a", 1)
    ka = recommend.profile_keywords("user_a")
    kb = recommend.profile_keywords("user_b")
    assert ka.get("attention", 0) != kb.get("attention", 0) or ka != kb


def test_recommend_endpoints(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr("app.agents.tools._github_repro",
                        lambda title: {"ok": True, "repos": []})
    client = TestClient(app)
    r = client.get("/api/recommendations", headers={"X-User-Id": "eu-1"},
                   params={"refresh": "true"})
    assert r.status_code == 200 and r.json()["recommendations"]
    rec_id = r.json()["recommendations"][0]["id"]
    r2 = client.post(f"/api/recommendations/{rec_id}/feedback",
                     headers={"X-User-Id": "eu-1"}, json={"vote": 1})
    assert r2.status_code == 200
    r3 = client.post(f"/api/recommendations/{rec_id}/feedback",
                     headers={"X-User-Id": "eu-1"}, json={"vote": 0})
    assert r3.status_code == 400
    r4 = client.get("/api/recommendations", headers={"X-User-Id": "x" * 200})
    assert r4.json()["user_id"] == "anonymous", "超长 user_id 护栏回退 anonymous"
