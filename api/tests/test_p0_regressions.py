"""Round2 P0 回归测试（先红后绿，验收方案 §3）。

P0-1 列表接口返回去重预警 · P0-2 版本解析+去重时序 · P0-3 长文档索引上限覆盖 summary
P0-4 Dockerfile 含 uv.lock · P0-6 Writer final 带 outline · P0-7 LLM ping 失败短 TTL+重试
P1-1 done 后立即可回放（无竞态 404）
"""
from __future__ import annotations

import io
import json
import os
import time
import uuid
from pathlib import Path

import pytest

# 密闭由 tests/conftest.py 会话级保证；本模块沿用其隔离目录
from tests.fixtures_pdf import make_paper_pdf  # noqa: E402

from app import db, jobs  # noqa: E402
from app.config import DOCIR_DIR  # noqa: E402
from app.llm import client as llm  # noqa: E402

_DATA = Path(os.environ["SCHOLAR_DATA_DIR"])


def _wait_job(client, job_id, timeout_s=180):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j.get("status") in ("done", "error"):
            return j
        time.sleep(0.3)
    return {"status": "timeout"}


def _upload(client, path: Path) -> dict:
    r = client.post("/api/papers",
                    files={"file": (path.name, io.BytesIO(path.read_bytes()), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def _two_version_pdfs() -> tuple[Path, Path]:
    tmp = _DATA / "p0"
    tmp.mkdir(parents=True, exist_ok=True)
    v7p, v1p = tmp / "1706.03762v7.pdf", tmp / "1706.03762v1.pdf"
    make_paper_pdf(v7p, "P0 Regression Paper",
                   body_facts=["Version 7 trains on 8 GPUs reaching 27.3 BLEU."],
                   refs=["Vaswani et al. 2017. arXiv:1706.03762"])
    make_paper_pdf(v1p, "P0 Regression Paper",
                   body_facts=["Version 1 trains on 8 GPUs reaching 26.1 BLEU."],
                   refs=["Vaswani et al. 2017. arXiv:1706.03762"])
    return v7p, v1p


# ---------- P0-1 列表接口返回去重预警 ----------

def test_p0_1_dedup_alert_in_list():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    db.init_db()
    v7p, v1p = _two_version_pdfs()
    p1 = _upload(client, v7p)
    _wait_job(client, p1.get("job_id"))
    p2 = _upload(client, v1p)
    _wait_job(client, p2.get("job_id"))
    listing = client.get("/api/papers").json()
    alerted = [p for p in listing.get("papers", []) if p.get("dedup_alert")]
    assert alerted, "列表接口应携带 dedup_alert（前端黄色预警条数据源）"
    payload = json.dumps(alerted[0]["dedup_alert"], ensure_ascii=False)
    assert "建议保留" in payload


# ---------- P0-2 版本解析 + 去重时序 ----------

def test_p0_2_version_from_filename():
    assert jobs._version_from_name("1706.03762v7.pdf") == "v7"
    assert jobs._version_from_name("1706.03762v1.pdf") == "v1"
    assert jobs._version_from_name(" UnderstandingDeepLearning_02_09_26_C.pdf") in ("", "v1")


def test_p0_2_dedup_alert_before_job_done():
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    db.init_db()
    v7p, v1p = _two_version_pdfs()
    p1 = _upload(client, v7p)
    _wait_job(client, p1.get("job_id"))
    p2 = _upload(client, v1p)
    j = _wait_job(client, p2.get("job_id"))
    assert j["status"] == "done"
    rows = db.query("SELECT payload FROM jobs WHERE kind='dedup_alert' AND paper_id=? "
                    "ORDER BY created_at DESC LIMIT 1", (p2["paper_id"],))
    assert rows, "parse done 的瞬间 dedup_alert 必须已存在（先去重后置 done）"
    payload = json.dumps(json.loads(rows[0]["payload"]), ensure_ascii=False)
    assert "建议保留" in payload and "v7" in payload


# ---------- P0-3 长文档索引上限覆盖 summary 块 ----------

def test_p0_3_summary_chunk_within_index_cap():
    """toc 后段章节 → summary 块也必须被 cap 过滤（max chunk page < cap）。"""
    docir = {
        "meta": {"title": "Long", "page_count": 300},
        "toc": [
            {"level": 1, "title": "Intro", "page": 0},
            {"level": 1, "title": "Far Chapter", "page": 200},   # cap 之外
        ],
        "blocks": [
            {"id": "b1", "type": "paragraph", "text": "Intro body text. " * 20,
             "page": 0, "section_path": "Intro", "bbox": [0.1, 0.1, 0.9, 0.2]},
            {"id": "b2", "type": "paragraph", "text": "Far chapter body. " * 20,
             "page": 200, "section_path": "Far Chapter", "bbox": [0.1, 0.1, 0.9, 0.2]},
        ],
        "tables": [],
    }
    from app.rag.chunking import chunk_docir
    view = jobs._index_view(docir, cap=120)
    assert all(t.get("page", 0) < 120 for t in view["toc"]), "index 视图的 toc 必须被 cap 过滤"
    chunks = chunk_docir(view)
    assert chunks, "cap 内应有 chunk"
    assert max(c["page"] for c in chunks) < 120, "summary/detail 全部块都必须在索引上限内"


# ---------- P0-4 Dockerfile 含 uv.lock ----------

def test_p0_4_dockerfile_has_uvlock():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "uv.lock" in dockerfile, "uv sync --frozen 需要 uv.lock 先于 COPY（P0-4）"


# ---------- P0-6 Writer final 带 outline ----------

def test_p0_6_writer_final_contains_outline(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    # writer 需证据池 ≥3 块 → 直接构造 3 章节 DocIR 入库索引（不走 PDF 解析，精准可控）
    db.init_db()
    from app.rag import indexing as idx
    from app.parsing.router import write_docir
    from app.rag.chunking import chunk_docir
    pid = "p_p0_6"
    docir = {"meta": {"title": "Writer Outline Feed", "page_count": 3},
             "toc": [{"level": 1, "title": s, "page": i} for i, s in
                     enumerate(["Intro", "Method", "Results", "Discussion"])],
             "blocks": [{"id": f"b{i}", "type": "paragraph",
                         "text": f"Fact {name} detail. " * 15,
                         "page": i, "section_path": s,
                         "bbox": [0.1, 0.1, 0.9, 0.2]}
                        for i, (name, s) in
                        enumerate([("A about GPUs", "Intro"), ("B about BLEU", "Method"),
                       ("C about data", "Results"), ("D about training", "Discussion")])],
             "tables": []}
    write_docir(pid, docir)
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO papers(id,title,file_path,file_hash,created_at,"
                  "meta_json,docir_path) VALUES(?,?,?,?,?,?,?)",
                  (pid, "Writer Outline Feed", "", f"h_{pid}", time.time(), "{}",
                   str(Path(DOCIR_DIR) / f"{pid}.json")))
    idx.index_chunks(pid, chunk_docir(docir))
    client = TestClient(app)
    p = {"paper_id": pid}

    fake_answer = ("**背景**\n Fact A [E1].\n\n**要点**\n Fact B [E1].\n\n**结论**\n Fact C [E1].")
    monkeypatch.setattr(llm, "available", lambda force=False: True)
    monkeypatch.setattr(llm, "_endpoint", lambda: ("stub", "k", "stub"))
    monkeypatch.setattr(llm, "stream_chat", lambda msgs, **kw: iter([fake_answer]))
    monkeypatch.setattr(llm, "chat", lambda msgs, **kw:
                        {"text": json.dumps({"claims": [{"text": "Fact A", "evidence_ids": [1],
                                                         "quote": "Fact A"}]}), "model": "stub"})

    events: list[tuple[str, dict]] = []
    with client.stream("POST", "/api/chat",
                       json={"question": "write a short essay about GPUs BLEU data", "paper_ids": [p["paper_id"]],
                             "conversation_id": f"p0_6_{uuid.uuid4().hex[:6]}", "agent": "writer"}) as r:
        assert r.status_code == 200
        event = ""
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: ") and event:
                events.append((event, json.loads(line[6:])))
    final = next((d for k, d in events if k == "final"), None)
    assert final is not None, "writer 流应有 final"
    assert final.get("outline"), "final 必须携带 outline（前端 OutlineView 数据源，P0-6）"
    assert final["outline"].get("sections"), "outline.sections 非空"


# ---------- P0-7 LLM ping 失败短 TTL + 重试 ----------

def test_p0_7_ping_retry_then_available(monkeypatch):
    calls = {"n": 0}

    def flaky_chat(msgs, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"text": "", "error": "transient"}
        return {"text": "pong", "model": "stub"}

    monkeypatch.setattr(llm, "chat", flaky_chat)
    monkeypatch.setattr(llm, "_endpoint", lambda: ("stub", "k", "stub"))
    monkeypatch.setattr(llm, "_ping_cache", {"ts": 0.0, "ok": False})
    assert llm.available(force=True) is True, "第一次 ping 失败应重试后成功（P0-7）"
    assert calls["n"] == 2

    # 失败缓存短 TTL（30s）：失败后短时间内不重探测，31s 后重新探测
    calls["n"] = 0

    def always_fail(msgs, **kw):
        calls["n"] += 1
        return {"text": "", "error": "down"}

    monkeypatch.setattr(llm, "chat", always_fail)
    monkeypatch.setattr(llm, "_ping_cache", {"ts": 0.0, "ok": False})
    monkeypatch.setattr(llm.time, "time", lambda: 1000.0)
    assert llm.available(force=True) is False
    assert calls["n"] >= 1
    calls["n"] = 0
    monkeypatch.setattr(llm.time, "time", lambda: 1000.0 + 20)   # 20s < 短 TTL → 不重探
    assert llm.available() is False and calls["n"] == 0, "失败后 20s 内应复用缓存（短 TTL）"
    monkeypatch.setattr(llm.time, "time", lambda: 1000.0 + 31)   # > 30s 短 TTL → 重新探测
    assert llm.available() is False and calls["n"] >= 1, "失败缓存过期后应重新探测"


# ---------- P1-1 done 后立即可回放 ----------

def test_p1_1_trace_replay_immediately_after_done(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    db.init_db()
    tmp = _DATA / "p0"
    tmp.mkdir(parents=True, exist_ok=True)
    pdf = tmp / "replay_feed.pdf"
    make_paper_pdf(pdf, "Replay Feed",
                   body_facts=["The replay test trains on 8 P100 GPUs."],
                   refs=["Ref One 2017."])
    client = TestClient(app)
    p = _upload(client, pdf)
    _wait_job(client, p.get("job_id"))

    fake_answer = "Trained on 8 P100 GPUs [E1]."
    monkeypatch.setattr(llm, "available", lambda force=False: True)
    monkeypatch.setattr(llm, "_endpoint", lambda: ("stub", "k", "stub"))
    monkeypatch.setattr(llm, "stream_chat", lambda msgs, **kw: iter([fake_answer]))
    monkeypatch.setattr(llm, "chat", lambda msgs, **kw:
                        {"text": json.dumps({"claims": [{"text": "8 GPUs", "evidence_ids": [1],
                                                         "quote": "8 P100 GPUs"}]}), "model": "stub"})

    conv = f"p1_1_{uuid.uuid4().hex[:6]}"
    final = None
    with client.stream("POST", "/api/chat",
                       json={"question": "几块 GPU？", "paper_ids": [p["paper_id"]],
                             "conversation_id": conv, "agent": "reader"}) as r:
        assert r.status_code == 200
        event = ""
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: ") and event:
                d = json.loads(line[6:])
                if event == "final":
                    final = d
    assert final and final.get("thread_id"), "final 应携带 thread_id"
    tid = final["thread_id"]
    # done 已收到的瞬间立即回放：不允许 404（竞态）
    for _ in range(5):
        r = client.get(f"/api/qa/{tid}")
        assert r.status_code == 200, f"done 后立即回放必须 200（竞态 404），got {r.status_code}"
        assert r.json().get("events"), "回放应带完整事件流"
