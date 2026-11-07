"""E2E 回归：上传 → 解析 → 索引 → 溯源问答 → 去重 → 审计日志（官方用例回归基线）。"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SCHOLAR_DATA_DIR", str(Path(__file__).parent / f"_data_{uuid.uuid4().hex[:8]}"))
# 测试密闭性：屏蔽一切真实 LLM/Embedding 凭据（机器全局 env 与项目 .env 都不进测试）
os.environ["LLM_PROVIDER"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["MINIMAX_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["EMBEDDING_BACKEND"] = "off"
os.environ["EMBEDDING_API_KEY"] = ""

from app.main import app  # noqa: E402  （必须在 env 之后导入）
from tests.fixtures_pdf import make_paper_pdf  # noqa: E402

TITLE = "Attention Is All You Need"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:  # lifespan 里 init_db
        yield c


def wait_done(client: TestClient, job_id: str, timeout: float = 60.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.3)
    raise TimeoutError(f"job {job_id} not done: {job}")


def test_1_upload_and_parse(client: TestClient):
    pdf = Path(os.environ["SCHOLAR_DATA_DIR"]) / "v7.pdf"
    make_paper_pdf(
        pdf, TITLE,
        body_facts=["The big model was trained on 8 NVIDIA P100 GPUs and took 3.5 days.",
                    "The base model reaches 27.3 BLEU on WMT 2014 English-to-German."],
        refs=["Vaswani et al. 2017. Attention Is All You Need. arXiv:1706.03762",
              "Bahdanau et al. 2015. Neural machine translation by jointly learning to align and translate."],
    )
    r = client.post("/api/papers", files={"file": ("1706.03762v7.pdf", pdf.read_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    job = wait_done(client, data["job_id"])
    assert job["status"] == "done", job["message"]

    paper = client.get(f"/api/papers/{data['paper_id']}").json()
    assert paper["title"] == TITLE
    assert paper["arxiv_id"] == "1706.03762"
    assert paper["page_count"] == 3
    assert paper["references_count"] == 2

    docir = client.get(f"/api/papers/{data['paper_id']}/docir").json()
    assert docir["toc"], "标题树为空"
    assert any(b["type"] == "paragraph" for b in docir["blocks"])
    # 双栏阅读顺序：第 2 页左栏事实先于右栏
    page2 = [b for b in docir["blocks"] if b["page"] == 1]
    texts = [b["text"] for b in page2]
    joined = " ".join(texts)
    assert "P100" in joined and "BLEU" in joined
    # 溯源锚点完整
    for b in docir["blocks"]:
        assert len(b["bbox"]) == 4 and b["bbox"][0] <= b["bbox"][2]

    # FTS 索引可检索
    from app.rag import indexing
    hits = indexing.fts_search([data["paper_id"]], "GPUs trained", top_k=5)
    assert hits and "P100" in hits[0]["text"]

    globals()["PAPER_ID"] = data["paper_id"]


def test_2_qa_with_citations(client: TestClient):
    paper_id = globals().get("PAPER_ID")
    with client.stream("POST", "/api/chat", json={"question": "How many GPUs was the model trained on?",
                                                   "paper_ids": [paper_id]}) as resp:
        assert resp.status_code == 200
        events = []
        for line in resp.iter_lines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: ") and ev:
                events.append((ev, json.loads(line[6:])))
    kinds = [e for e, _ in events]
    assert "retrieval" in kinds and "final" in kinds and "done" in kinds
    retrieval = next(d for e, d in events if e == "retrieval")
    final = next(d for e, d in events if e == "final")
    assert retrieval["hits"], "检索无命中"
    assert final["answer"], "答案为空"
    # 无 LLM 环境 → 抽取式证据模式也必须逐句挂锚点
    for c in final["claims"]:
        assert c["support"], f"论断缺少支撑: {c['text'][:50]}"
    if final["mode"] == "extractive":
        assert "[^p" in final["answer"]


def test_3_semantic_dedup(client: TestClient):
    """官方用例 1：同标题不同内容（v1 后传）→ 内容级预警。"""
    pdf = Path(os.environ["SCHOLAR_DATA_DIR"]) / "v1.pdf"
    make_paper_pdf(
        pdf, TITLE,
        body_facts=["The model was trained on 8 NVIDIA P100 GPUs.",
                    "We reach 26.1 BLEU on English-to-German translation."],
        refs=["Vaswani et al. 2017. Attention Is All You Need. arXiv:1706.03762"],
        arxiv_line="arXiv:1706.03762v1",
    )
    r = client.post("/api/papers", files={"file": ("1706.03762v1.pdf", pdf.read_bytes(), "application/pdf")})
    data = r.json()
    assert data.get("dedup") is None  # 文件 hash 不同 → 允许入库，内容级查重在解析后
    job = wait_done(client, data["job_id"])
    assert job["status"] == "done"

    paper = client.get(f"/api/papers/{data['paper_id']}").json()
    assert paper.get("dedup_alert"), "未触发内容级去重预警"
    assert paper["dedup_alert"]["duplicate_of"] == globals()["PAPER_ID"]


def test_4_audit_log(client: TestClient):
    logs = client.get("/api/audit/prompts").json()
    assert logs["logs"], "审计日志为空"
    # 测试环境屏蔽全部凭据（密闭性）：configured/available 都必须为 False（降级路径生效）
    assert logs["llm_configured"] is False
    assert logs["llm_available"] is False


def test_5_health(client: TestClient):
    h = client.get("/api/health").json()
    assert h["status"] == "ok"
    assert h["papers"] >= 2
