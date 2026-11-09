"""Round4 横切生产化测试：令牌桶限流 / request-id / /api/metrics / 上传魔数 /
匿名多用户隔离（profiles/recommendations/feedback 互不可见）。

口径：匿名多用户画像（X-User-Id 隔离 + 限流），非多租户鉴权——如实标注。
"""
from __future__ import annotations

import time
import uuid

from fastapi.testclient import TestClient

from app import db
from app.main import _limiter, app
from app.ratelimit import RateLimiter

client = TestClient(app)


# ---------- 令牌桶 ----------

def test_token_bucket_burst_and_refill():
    lim = RateLimiter(3)             # 3 次/分 → 桶容量 3
    assert lim.allow("u")[0] is True
    assert lim.allow("u")[0] is True
    assert lim.allow("u")[0] is True
    ok, retry = lim.allow("u")       # 第 4 次 → 拒绝 + 建议等待
    assert ok is False and 0 < retry <= 60
    assert lim.allow("other")[0] is True, "跨用户独立分桶"
    lim2 = RateLimiter(0)
    assert lim2.allow("u")[0] is True, "rate=0 恒放行（关闭档）"


def test_token_bucket_refill_over_time(monkeypatch):
    lim = RateLimiter(2)             # 2/min → 容量 2，回填 1 枚需 30s
    assert lim.allow("u")[0] is True
    assert lim.allow("u")[0] is True
    assert lim.allow("u")[0] is False
    t = {"now": time.monotonic()}

    def fast_forward():
        t["now"] += 35.0             # 快进 35s > 30s → 回填 ≥1 枚
        return t["now"]
    monkeypatch.setattr("app.ratelimit.time.monotonic", fast_forward)
    assert lim.allow("u")[0] is True, "时间推进后令牌回填"


# ---------- 中间件：request-id + 限流 429 + metrics 计数 ----------

def test_request_id_echo_and_generation():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.headers.get("X-Request-Id"), "自动生成 request-id 并回写"
    r2 = client.get("/api/health", headers={"X-Request-Id": "rid-abc"})
    assert r2.headers.get("X-Request-Id") == "rid-abc", "透传调用方 request-id"


def test_heavy_post_rate_limited_429(monkeypatch):
    """burst 超限 → 429 + Retry-After；跨用户不受牵连；窗口回填恢复。"""
    _limiter.rate = 2
    clock = {"now": time.monotonic()}
    monkeypatch.setattr("app.ratelimit.time.monotonic", lambda: clock["now"])
    try:
        _limiter.reset()
        path = "/api/papers/batch"   # 重端点 POST（空文件列表 → 400，同样经过限流层）
        # 同一用户连发 3 次：前 2 次过限流（业务 400 不影响），第 3 次 429
        codes = [client.post(path, headers={"X-User-Id": "u_rate"},
                             files=[]).status_code for _ in range(3)]
        assert codes[:2] != [429, 429], "前两次应放行（业务码任意）"
        assert codes[2] == 429, "第三次应 429"
        r = client.post(path, headers={"X-User-Id": "u_rate"}, files=[])
        assert r.status_code == 429
        assert r.headers.get("Retry-After") and r.headers.get("X-RateLimit-Limit") == "2"
        # 跨用户独立
        assert client.post(path, headers={"X-User-Id": "u_other"},
                           files=[]).status_code != 429
        # 时间回填（2/min → 回填 1 枚需 30s）：快进 35s 后放行
        clock["now"] += 35.0
        assert client.post(path, headers={"X-User-Id": "u_rate"},
                           files=[]).status_code != 429
    finally:
        monkeypatch.undo()
        _limiter.reset()
        _limiter.rate = 0            # 恢复密闭关闭档


def test_metrics_endpoint_counts():
    r = client.get("/api/metrics")
    assert r.status_code == 200
    m = r.json()
    assert m["requests"]["total"] > 0
    assert set(m) >= {"uptime_s", "requests", "llm", "jobs", "papers", "translations", "process"}
    assert m["requests"]["by_status"].get("200", 0) > 0
    # rate_limited 计数已在上面的 429 测试中累计
    assert m["requests"]["rate_limited"] >= 1


# ---------- 安全体检 ----------

def test_upload_rejects_fake_pdf_magic():
    """伪装 .pdf 后缀的非 PDF → 400（魔数护栏；此前会 500/入库）。"""
    r = client.post("/api/papers",
                    files={"file": ("evil.pdf", b"#!/bin/sh\ncat /etc/passwd\n" * 40,
                                    "application/pdf")})
    assert r.status_code == 400
    assert "not_a_pdf" in r.json()["detail"]


def test_sandbox_figure_path_traversal_rejected():
    for name in ("..%2f..%2fetc%2fpasswd", "..\\..\\app.db", "abc.png", "zzz.png"):
        r = client.get(f"/api/sandbox/figure/{name}")
        assert r.status_code in (400, 404), f"{name} 不应 200"


# ---------- 匿名多用户隔离 ----------

def test_user_isolation_profiles_and_recommendations():
    """两个匿名用户：画像/推荐/反馈互不可见（按 user_id 隔离）。"""
    from app.agents import recommend
    db.init_db()
    u1, u2 = f"iso_{uuid.uuid4().hex[:6]}", f"iso_{uuid.uuid4().hex[:6]}"
    with db.tx() as c:
        c.execute("DELETE FROM tracked_items")
        c.execute("INSERT INTO tracked_items(id,sub_id,arxiv_id,title,published,authors,"
                  "abstract,status,added_at) VALUES('ti_iso','ts_iso','2401.99999',"
                  "'Deep Attention Survey','2026-11-01','[]','attention models','new',?)",
                  (time.time(),))
    try:
        # 基线先取：画像=库标题词 ∪ 个人反馈——库词全用户共享（by design），
        # 隔离断言用「u1 反馈前后 u2 画像不变」而非词不存在（防库词污染误报）
        p2_before = dict(recommend.profile_keywords(u2))
        recommend.generate(u1, 5, with_github=False)
        rec = db.query("SELECT id FROM recommendations WHERE user_id=?", (u1,))[0]
        recommend.record_feedback(rec["id"], u1, 1)
        p1 = recommend.profile_keywords(u1)
        p2_after = recommend.profile_keywords(u2)
        assert p2_after == p2_before, "u1 的反馈不得污染 u2 画像"
        assert any(k in p1 for k in ("attention", "deep", "survey")), "u1 画像应含候选词"
        assert not db.query("SELECT 1 FROM recommendations WHERE user_id=?", (u2,)), \
            "u1 的推荐不出现在 u2 名下"
        # 端点层：X-User-Id 不同 → 各自的列表
        r1 = client.get("/api/recommendations", headers={"X-User-Id": u1})
        r2 = client.get("/api/recommendations", headers={"X-User-Id": u2})
        assert r1.json()["user_id"] == u1 and r2.json()["user_id"] == u2
    finally:
        with db.tx() as c:
            for u in (u1, u2):
                c.execute("DELETE FROM user_profiles WHERE user_id=?", (u,))
                c.execute("DELETE FROM recommendations WHERE user_id=?", (u,))
                c.execute("DELETE FROM recommend_feedback WHERE user_id=?", (u,))
            c.execute("DELETE FROM tracked_items WHERE id='ti_iso'")

