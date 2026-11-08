"""模块一 P0 批量上传：单文件失败隔离、逐文件独立结果、精确去重复用。"""
from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient

from tests.fixtures_pdf import make_paper_pdf

_DATA = Path(__import__("os").environ["SCHOLAR_DATA_DIR"])


def _client() -> TestClient:
    from app.main import app

    return TestClient(app)


def _pdf(name: str, title: str) -> tuple[str, bytes]:
    p = _DATA / name
    make_paper_pdf(p, title, body_facts=[f"Unique body fact for {title}."],
                   refs=["R1 et al. 2020. arXiv:2001.00001"], arxiv_line="arXiv:2001.00001v1")
    return name, p.read_bytes()


def test_batch_mixed_success_and_failure():
    client = _client()
    f1 = _pdf("bt_a.pdf", "Batch Paper Alpha")
    f2 = _pdf("bt_b.pdf", "Batch Paper Beta")
    files = [("files", (f1[0], io.BytesIO(f1[1]), "application/pdf")),
             ("files", ("broken.txt", io.BytesIO(b"not a pdf"), "text/plain")),
             ("files", (f2[0], io.BytesIO(f2[1]), "application/pdf"))]
    r = client.post("/api/papers/batch", files=files)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["results"]) == 3
    assert data["ok_count"] == 2, "单个失败不影响其他（批量 P0 硬要求）"
    bad = next(x for x in data["results"] if x["filename"] == "broken.txt")
    assert bad.get("error") and not bad.get("job_id")
    for good in data["results"]:
        if good is not bad:
            assert good.get("job_id"), f"成功文件应有独立解析任务：{good}"


def test_batch_exact_file_reuse():
    client = _client()
    f1 = _pdf("bt_c.pdf", "Batch Paper Gamma")
    body = f1[1]
    r1 = client.post("/api/papers/batch",
                     files=[("files", (f1[0], io.BytesIO(body), "application/pdf"))])
    assert r1.status_code == 200
    r2 = client.post("/api/papers/batch",
                     files=[("files", ("again.pdf", io.BytesIO(body), "application/pdf"))])
    item = r2.json()["results"][0]
    assert item.get("dedup") == "exact_file", "同文件批内精确去重复用"
    assert item.get("job_id") is None


def test_batch_over_limit_rejected():
    client = _client()
    files = [("files", (f"ov{i}.pdf", io.BytesIO(b"x"), "application/pdf")) for i in range(21)]
    r = client.post("/api/papers/batch", files=files)
    assert r.status_code == 413


def test_cancel_job_404_and_job_shape():
    client = _client()
    assert client.post("/api/jobs/nonexistent/cancel").status_code == 404
    assert client.get("/api/jobs/nonexistent").status_code == 404
