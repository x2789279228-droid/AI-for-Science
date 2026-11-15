"""健康报告聚合端点(/api/health-report)口径测试。

2026-11-15 重写后口径:
- 只统计最近 7 天"真实用户轨迹"(排除 kappa_/gold_/bench_/e2e_ 等评测/测试前缀);
- 失败 trace / 失败任务进可靠性分母;拒答/无答案单独计入"回答覆盖";
- 7 个固定权重子分,缺失维度按 0 计且不重归一化;
- 样本 < 30 时不出总分;
- 最新离线金标 gates 未全过时总分封顶 0.90。
"""
import time

from fastapi.testclient import TestClient


def _seed_trace(thread_id: str, claims: list, status: str = "done",
                mode: str = "generative") -> None:
    from app import trace_store
    trace_store.init()
    final = {"answer": "a", "elapsed": 1.0, "mode": mode, "claims": claims}
    trace_store.append_trace(thread_id, "conv-hr", 0, "问题", status,
                             [("plan", {"round": 1, "steps": []}),
                              ("verifier", {"verdict": "pass", "evidence_count": 3}),
                              ("final", final)])


def _get() -> dict:
    from app.main import app
    return TestClient(app).get("/api/health-report").json()


def test_health_report_structure():
    d = _get()
    for key in ("total", "runtime_total", "scores", "samples", "window", "missing",
                "trace", "qa_perf", "parse", "dedup", "offline", "external"):
        assert key in d, key
    assert d["window"]["days"] == 7 and d["window"]["min_traces"] == 30
    assert set(d["scores"]) <= set(d["weights"])
    assert set(d["missing"]) == set(d["weights"]) - set(d["scores"])
    for s in d["scores"].values():
        assert "value" in s and "weight" in s and "formula" in s
        assert 0 <= s["value"] <= 1
    assert d["total"] is None or 0 <= d["total"] <= 1
    assert "claims" in d["trace"]


def test_eval_and_test_traces_excluded_from_production_sample():
    before = _get()
    _seed_trace("kappa_hr_eval", [{"status": "unsupported"}] * 3)
    _seed_trace("gold_hr_eval", [{"status": "unsupported"}] * 2)
    _seed_trace("4f3c2a1b8e7d4c6f9a0b1c2d3e4f5a6b:1",
                [{"status": "supported"}])
    after = _get()
    # 只多出 1 条真实轨迹；评测轨迹既不进 samples 也不进 claims
    assert after["samples"]["traces"] == before["samples"]["traces"] + 1
    assert after["trace"]["claims"]["unsupported"] == before["trace"]["claims"]["unsupported"]
    assert after["trace"]["claims"]["supported"] == before["trace"]["claims"]["supported"] + 1


def test_failed_trace_penalizes_reliability_and_coverage():
    before = _get()
    _seed_trace("9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d:1", [], status="failed",
                mode="none")
    after = _get()
    assert after["samples"]["traces"] == before["samples"]["traces"] + 1
    assert after["samples"]["failed"] == before["samples"]["failed"] + 1
    assert after["samples"]["no_answer"] == before["samples"]["no_answer"] + 1
    # 失败进可靠性分母（不是被排除后抬分）
    if before.get("scores", {}).get("reliability") and after["scores"].get("reliability"):
        assert after["scores"]["reliability"]["value"] < before["scores"]["reliability"]["value"]
    if before.get("scores", {}).get("answer_coverage") and after["scores"].get("answer_coverage"):
        assert after["scores"]["answer_coverage"]["value"] <= \
            before["scores"]["answer_coverage"]["value"]


def test_fixed_weights_no_renormalize_when_metric_missing(monkeypatch):
    """缺失维度按 0 计：runtime_total = Σ(value×weight)，权重不被重新归一化。"""
    from app.api_routes import routes
    orig_query = routes.db.query

    def fake_query(sql, *args, **kwargs):
        if "latency_ms FROM qa_logs" in sql:
            return []           # 强制 performance 缺失
        return orig_query(sql, *args, **kwargs)

    monkeypatch.setattr(routes.db, "query", fake_query)
    monkeypatch.setattr(routes, "_HEALTH_MIN_TRACES", 1)
    d = _get()
    assert "performance" in d["missing"]
    assert "performance" not in d["scores"]
    used = sum(s["weight"] for s in d["scores"].values())
    assert used < 1.0                  # 缺失维度的权重不被分摊
    expected = sum(s["value"] * s["weight"] for s in d["scores"].values())
    assert abs(d["runtime_total"] - expected) < 1e-3


def test_performance_uses_qa_module_and_window():
    from app import db

    cutoff = time.time() - 7 * 86400
    old_ts = time.time() - 10 * 86400
    now = time.time()
    with db.tx() as c:
        # 20 条窗口内 qa 调用（1000..2900ms）；窗口外/非问答模块的慢调用不得污染 p50/p95
        for v in range(1000, 3000, 100):
            c.execute("INSERT INTO qa_logs(ts,module,latency_ms,guardrail) "
                      "VALUES(?,?,?,?)", (now, "qa", float(v), ""))
        c.execute("INSERT INTO qa_logs(ts,module,latency_ms,guardrail) VALUES(?,?,?,?)",
                  (old_ts, "qa", 99000.0, ""))
        c.execute("INSERT INTO qa_logs(ts,module,latency_ms,guardrail) VALUES(?,?,?,?)",
                  (now, "translate", 88000.0, ""))
    # 与端点同口径从 DB 复算期望值（会话内其他用例可能也写过 qa_logs）
    rows = db.query(
        "SELECT latency_ms FROM qa_logs WHERE CAST(ts AS REAL) >= ? "
        "AND module IN ('qa', 'chat') AND latency_ms > 0 ORDER BY latency_ms",
        (cutoff,))
    vals = [float(r["latency_ms"]) for r in rows]
    assert len(vals) >= 20
    import math
    exp_p50 = vals[math.ceil(0.50 * len(vals)) - 1]
    exp_p95 = vals[math.ceil(0.95 * len(vals)) - 1]
    d = _get()
    perf = d["scores"].get("performance")
    assert perf is not None
    assert perf["raw"]["p50_ms"] == int(exp_p50)
    assert perf["raw"]["p95_ms"] == int(exp_p95)
    assert d["qa_perf"]["count"] == len(vals)      # 只统计窗口内 qa/chat
    assert d["window"]["days"] == 7


def test_external_eval_gate_cap(monkeypatch):
    from app.api_routes import routes

    monkeypatch.setattr(routes, "_HEALTH_MIN_TRACES", 1)
    monkeypatch.setattr(routes, "_latest_eval_gold", lambda: (
        "test-round",
        {"time": "2026-11-15 12:00:00",
         "gates": {"recall": True, "compare_ok": False},
         "generative": {"hallucination_rate": 0.0},
         "recall_at5": 0.9},
    ))
    d = _get()
    assert d["external"]["available"] is True
    assert d["external"]["gates_passed"] == 1
    assert d["external"]["gates_total"] == 2
    assert d["external"]["gates_failed"] == ["compare_ok"]
    assert d["cap"] == 0.9
    assert d["total"] is not None and d["total"] <= 0.9 + 1e-9
    if d["runtime_total"] > 0.9:
        assert d["cap_applied"] is True
        assert abs(d["total"] - 0.9) < 1e-9


def test_insufficient_sample_no_total(monkeypatch):
    from app.api_routes import routes
    monkeypatch.setattr(routes, "_HEALTH_MIN_TRACES", 10_000)
    d = _get()
    assert d["insufficient"] is True
    assert d["total"] is None
    assert "最小样本" in d["insufficient_reason"]


def test_parse_quality_coverage_counts_papers(monkeypatch):
    """parse 覆盖计数 = 有质量元数据的论文数（不再固定为 len(parts)=2）。"""
    from app import db
    from app.api_routes import routes

    with db.tx() as c:
        c.execute("INSERT INTO papers(id,title,file_path,file_hash,page_count,"
                  "created_at,docir_path,status) VALUES(?,?,?,?,?,?,?,?)",
                  ("p_hr_parse", "测试论文", "hr.pdf", "hrhash", 10,
                   time.time(), "hr.json", "active"))
    monkeypatch.setattr(routes, "_resolve_paper_data", lambda pid: {
        "meta": {"quality": {"text_coverage": 0.9, "reference_parse_rate": 0.8}}})
    d = _get()
    p = d["parse"]
    assert p is not None
    assert p["papers_with_quality"] == p["papers_total"] >= 1
    assert p["page_weighted"] == 0.85
    assert abs(p["value"] - round(p["page_weighted"] * p["quality_coverage"], 4)) < 1e-6
