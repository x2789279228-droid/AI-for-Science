"""4.3 解析收尾测试（密闭）：demo_raw_order 素材 / outline 按需加载 / 内存曲线。"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SCHOLAR_DATA_DIR", str(Path(__file__).parent / f"_p43_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_PROVIDER"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["MINIMAX_API_KEY"] = ""
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["MINERU_ENABLED"] = "off"
os.environ["EMBEDDING_BACKEND"] = "off"

from app import db, jobs
from app.config import DOCIR_DIR
from app.parsing.router import parse_document
from tests.fixtures_pdf import make_paper_pdf

DATA = Path(os.environ["SCHOLAR_DATA_DIR"])


@pytest.fixture(scope="module")
def vector_pdf() -> Path:
    p = DATA / "vec43.pdf"
    make_paper_pdf(p, "Two Column Demo",
                   body_facts=["The model was trained on 8 NVIDIA P100 GPUs.",
                               "It reaches 27.3 BLEU on the benchmark."],
                   refs=["Sample Ref. 2017. arXiv:1706.03762"])
    return p


def test_demo_raw_order_captured(vector_pdf):
    docir = parse_document(str(vector_pdf))
    raw = docir["meta"].get("demo_raw_order")
    assert raw, "PyMuPDF 路径应捕获第 2 页原始块序（阅读顺序红绿开关素材）"
    assert raw["page"] == 2
    assert isinstance(raw["x0_order"], list) and len(raw["x0_order"]) >= 2


def test_memory_curve_in_parse(vector_pdf):
    """解析期间 psutil 采样 → meta.parse_memory.peak_mb > 0。"""
    db.init_db()
    pid = "p_mem43"
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO papers(id,title,file_path,file_hash,created_at,meta_json)"
                  " VALUES(?,?,?,?,?,?)", (pid, "Mem", str(vector_pdf), "h_mem", time.time(), "{}"))
    job_id = jobs.submit_parse(pid)
    for _ in range(120):   # 轮询至 done（本地 fixture 秒级）
        j = db.get_job(job_id)
        if j and j["status"] in ("done", "error"):
            break
        time.sleep(0.5)
    assert j["status"] == "done", j["message"]
    docir = json.loads((Path(DOCIR_DIR) / f"{pid}.json").read_text(encoding="utf-8"))
    mem = docir["meta"].get("parse_memory")
    assert mem and mem["peak_mb"] > 0 and mem["peak_mb"] < 2048   # <2GB 约束
    assert mem["samples"] >= 1


def test_outline_endpoint_light_payload(vector_pdf):
    """?outline=1：大纲级载荷（几 KB，不含 blocks/tables 正文）。"""
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)
    pid = "p_out43"
    db.init_db()
    docir = parse_document(str(vector_pdf))
    (Path(DOCIR_DIR) / f"{pid}.json").write_text(json.dumps(docir), encoding="utf-8")
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO papers(id,title,file_path,file_hash,created_at,"
                  "meta_json,docir_path) VALUES(?,?,?,?,?,?,?)",
                  (pid, "Outline", str(vector_pdf), "h_out", time.time(), "{}",
                   str(Path(DOCIR_DIR) / f"{pid}.json")))

    r = client.get(f"/api/papers/{pid}/docir?outline=1")
    assert r.status_code == 200
    o = r.json()
    assert "meta" in o and "toc" in o and "references" in o
    assert isinstance(o["pages"], list) and o["block_count"] > 0
    assert "blocks" not in o                      # 正文不在大纲载荷里

    r2 = client.get(f"/api/papers/{pid}/docir?page=1")   # 0 基页码正文过滤仍可用
    assert r2.status_code == 200
    page_data = r2.json()
    assert all(b["page"] == 1 for b in page_data["blocks"])
    assert len(page_data["blocks"]) > 0
