# -*- coding: utf-8 -*-
"""qa/recent 轻列表（前端改造方案 v1.0 §3.3 唯一后端新增点）：
- 空库 → items==[]
- 两条 trace（final 带 claims）→ 最近在前、四态计数正确
- 路由顺序护栏：/qa/recent 不得被 /qa/{trace_id} 吞并

注意：trace_store.init() 在应用 lifespan 中调用，TestClient 直连请求不触发
lifespan——测试内显式 init（幂等，conftest 已隔离进程内 data 目录）。
"""
import time

from fastapi.testclient import TestClient


def _seed_trace(thread_id: str, question: str, claims: list, status: str = "done") -> None:
    from app import trace_store
    trace_store.init()
    final = {"answer": "a", "elapsed": 1.2, "claims": claims}
    # append_trace 口径:list[(kind, data)] → 落盘 {"kind","data"}(与 runner._save_trace 一致)
    trace_store.append_trace(thread_id, "conv-test", 0, question, status,
                             [("plan", {"note": "x"}), ("final", final)])


def test_qa_recent_empty():
    """全量套件跑时其他用例可能已写入 trace_events——先清表再断言空列表。"""
    from app import db, trace_store
    from app.main import app
    trace_store.init()
    with db.tx() as c:
        c.execute("DELETE FROM trace_events")
    client = TestClient(app)
    r = client.get("/api/qa/recent", params={"limit": 3})
    assert r.status_code == 200
    assert r.json() == {"items": []}


def test_qa_recent_counts_and_order():
    from app.main import app
    _seed_trace("t_qr_old", "旧问题",
                [{"status": "supported"}, {"status": "unsupported"}])
    time.sleep(0.02)   # updated_at 秒级浮点，保证 DESC 排序确定
    _seed_trace("t_qr_new", "新问题",
                [{"status": "supported"}, {"status": "needs_check"},
                 {"status": "absent"}, {"status": "supported"}])
    client = TestClient(app)
    r = client.get("/api/qa/recent", params={"limit": 3})
    assert r.status_code == 200
    items = r.json()["items"]
    ids = [i["thread_id"] for i in items]
    assert ids[:2] == ["t_qr_new", "t_qr_old"]
    top = items[0]
    assert top["question"] == "新问题"
    assert top["claims"] == {"supported": 2, "needs_check": 1,
                             "unsupported": 0, "absent": 1}
    assert top["elapsed"] == 1.2
    assert top["status"] == "done"


def test_qa_recent_route_not_shadowed():
    """/qa/recent 声明顺序护栏：即使库中无该 trace 也必须返回列表结构而非 404。"""
    from app import trace_store
    from app.main import app
    trace_store.init()
    client = TestClient(app)
    r = client.get("/api/qa/recent")
    assert r.status_code == 200
    assert "items" in r.json()
