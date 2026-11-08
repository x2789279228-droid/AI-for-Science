"""模块一 P0 DiffLens：章节对齐 / 段落 diff / 数值变化 / 抽取式摘要 / API 回路。

测试环境无 LLM → 摘要走 extractive 兜底（闭环不中断的降级路径本身也是被测对象）。
PDF 自建（fixtures_pdf 的版式固定，自定义标题进不了 toc 字号树）。
"""
from __future__ import annotations

import io
import time
from pathlib import Path

import fitz
from fastapi.testclient import TestClient

from app.rag.version_diff import align_sections, compute_diff, load_docir_for

_DATA = Path(__import__("os").environ["SCHOLAR_DATA_DIR"])


def _client() -> TestClient:
    from app.main import app

    return TestClient(app)


def _make(path: Path, title: str, steps: str, extra_section: bool) -> Path:
    """自建论文式 PDF：标题(18) > 节标题(13) > 正文(10)，保证字号树能识别章节。"""
    doc = fitz.open()
    page = doc.new_page()
    y = 60
    page.insert_text((72, y), title, fontsize=18, fontname="hebo")
    y += 30
    page.insert_text((72, y), "Abstract", fontsize=13, fontname="hebo")
    y += 18
    page.insert_text((72, y), "We study scaling behaviour of attention models.", fontsize=10)
    y += 26
    page.insert_text((72, y), "1 Introduction", fontsize=13, fontname="hebo")
    y += 18
    page.insert_text((72, y), "Attention models scale with data and compute.", fontsize=10)
    y += 26
    page.insert_text((72, y), "2 Training", fontsize=13, fontname="hebo")
    y += 18
    page.insert_text((72, y),
                     f"The model was trained for {steps} steps on 8 GPUs to convergence.",
                     fontsize=10)
    if extra_section:
        page2 = doc.new_page()
        page2.insert_text((72, 60), "3 Ablation", fontsize=13, fontname="hebo")
        page2.insert_text((72, 80),
                          "Removing positional encodings degrades BLEU by 2.1 points.",
                          fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def _wait(client: TestClient, job_id: str, timeout_s: float = 120) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            return j
        time.sleep(0.2)
    return {"status": "timeout"}


def test_align_sections_added():
    base = [{"level": 1, "title": "1 Introduction", "page": 0},
            {"level": 1, "title": "2 Training", "page": 1}]
    target = [{"level": 1, "title": "1 Introduction", "page": 0},
              {"level": 1, "title": "2 Training (Revised)", "page": 1},
              {"level": 1, "title": "3 Ablation", "page": 2}]
    out = align_sections(base, target)
    added = [s for s in out if s["status"] == "added"]
    aligned = [s for s in out if s["status"] == "aligned"]
    assert any("Ablation" in s["title"] for s in added), "新增章节必须识别"
    assert len(aligned) == 2, "改名节 ratio≥0.8 应对齐而非增删"


def test_compute_diff_numbers_and_stats():
    from app.parsing import pipeline

    tmp = _DATA / "vdiff"
    tmp.mkdir(parents=True, exist_ok=True)
    base = pipeline.parse_pdf(_make(tmp / "base.pdf", "DiffLens Base Title",
                                    "100K", extra_section=False))
    target = pipeline.parse_pdf(_make(tmp / "target.pdf", "DiffLens Base Title",
                                      "300K", extra_section=True))
    payload = compute_diff(base, target)
    stats = payload["stats"]
    assert stats["sections_added"] >= 1, "新增章节统计"
    assert stats["numbers_changed"] >= 2, "100K 移除 + 300K 新增"
    values = {n["value"].strip() for n in payload["numbers"]}
    assert any(v.startswith("300K") for v in values), f"数值新增应点名：{values}"
    assert payload["summary"]["engine"] == "extractive", "无 LLM → 抽取式兜底"
    assert len(payload["summary"]["bullets"]) >= 1, "降级摘要仍须有要点（闭环不中断）"
    # Top-N 限流结构
    assert len(payload["sections"]) <= 21


def test_version_diff_api_roundtrip():
    client = _client()
    tmp = _DATA / "vdiff_api"
    tmp.mkdir(parents=True, exist_ok=True)

    def up(path: Path) -> dict:
        r = client.post("/api/papers",
                        files={"file": (path.name, io.BytesIO(path.read_bytes()),
                                        "application/pdf")})
        assert r.status_code == 200, r.text
        return r.json()

    # 标题不同 → 不触发去重，专注 diff 链路
    b = up(_make(tmp / "v1.pdf", "Evolution Study VOne", "100K", extra_section=False))
    t = up(_make(tmp / "v2.pdf", "Evolution Study VTwo", "300K", extra_section=True))
    assert _wait(client, b["job_id"])["status"] == "done"
    assert _wait(client, t["job_id"])["status"] == "done"

    r = client.post(f"/api/papers/{t['paper_id']}/version-diff",
                    json={"base_id": b["paper_id"]})
    assert r.status_code == 200, r.text
    if r.json().get("job_id"):
        assert _wait(client, r.json()["job_id"])["status"] == "done"
    g = client.get(f"/api/papers/{t['paper_id']}/version-diff?base_id={b['paper_id']}")
    assert g.status_code == 200, "任务完成后缓存必须可读"
    payload = g.json()["payload"]
    assert payload["stats"]["numbers_changed"] >= 2
    assert any("Ablation" in s["title"] for s in payload["sections"]
               if s["status"] == "added")
    # 二次 POST 命中缓存
    r2 = client.post(f"/api/papers/{t['paper_id']}/version-diff",
                     json={"base_id": b["paper_id"]})
    assert r2.json().get("cached") is True
    assert load_docir_for(b["paper_id"]) is not None
