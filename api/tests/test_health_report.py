# -*- coding: utf-8 -*-
"""健康报告聚合端点(健康中心页数据源):
- 结构完整:total/scores/trace/qa_perf/dedup 键存在,scores 每项含 value+weight+formula
- 有 trace 数据时 trace_trust 子分出现且公式含原始计数
- 路由独立于 /api/health(后者仍是轻量 LLM 状态)
"""
from fastapi.testclient import TestClient


def _seed_trace(thread_id: str, question: str, claims: list) -> None:
    from app import trace_store
    trace_store.init()
    final = {"answer": "a", "elapsed": 1.0, "claims": claims}
    trace_store.append_trace(thread_id, "conv-hr", 0, question, "done",
                             [("plan", {"round": 1, "steps": []}),
                              ("verifier", {"verdict": "pass", "evidence_count": 3}),
                              ("final", final)])


def test_health_report_structure():
    from app import trace_store
    from app.main import app
    trace_store.init()
    _seed_trace("t_hr_1", "问题一",
                [{"status": "supported"}, {"status": "needs_check"},
                 {"status": "supported"}, {"status": "absent"}])
    client = TestClient(app)
    r = client.get("/api/health-report")
    assert r.status_code == 200
    d = r.json()
    for key in ("total", "scores", "trace", "qa_perf", "dedup"):
        assert key in d, key
    assert "trace_trust" in d["scores"]
    s = d["scores"]["trace_trust"]
    # 公式透明:含原始计数文本
    assert "supported" in s["formula"] and s["value"] > 0
    assert d["trace"]["claims"]["absent"] == 1


def test_health_report_distinct_from_light_health():
    """/api/health-report 与轻量 /api/health 是两个端点(后者不聚合)。"""
    from app.main import app
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health-report").status_code == 200
