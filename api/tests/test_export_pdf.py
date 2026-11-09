"""译文导出测试（模块三 P0-2，密闭）：overlay 原位替换 / bilingual 双页 / 公式不动 /
图表重叠跳过 / 溢出补遗 / MD/HTML / 端点护栏。"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import fitz
import pytest

os.environ.setdefault("SCHOLAR_DATA_DIR",
                      str(Path(__file__).parent / f"_exp_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_PROVIDER"] = ""
os.environ["MINERU_ENABLED"] = "off"
os.environ["EMBEDDING_BACKEND"] = "off"

from app import db
from app.config import DATA_DIR, DOCIR_DIR, UPLOAD_DIR
from app.export import pdf_export

PID = "p_exp"
W, H = 612.0, 792.0


def _norm(x0: float, y0: float, x1: float, y1: float) -> list[float]:
    return [round(x0 / W, 4), round(y0 / H, 4), round(x1 / W, 4), round(y1 / H, 4)]


@pytest.fixture(scope="module", autouse=True)
def _setup():
    db.init_db()
    DOCIR_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    pdf = DOCIR_DIR.parent / "uploads" / f"{PID}.pdf"
    doc = fitz.open()
    p0 = doc.new_page(width=W, height=H)
    p0.insert_text(fitz.Point(72, 88), "The encoder uses attention.", fontsize=11)
    p0.insert_text(fitz.Point(72, 300), "$E=mc^2$", fontsize=11)          # 公式块（不替换）
    p0.insert_text(fitz.Point(72, 500), "Overlaps a table region.", fontsize=11)  # 与表重叠 → 跳过
    p1 = doc.new_page(width=W, height=H)
    p1.insert_text(fitz.Point(72, 88), "Training took 3.5 days.", fontsize=11)
    doc.save(str(pdf)); doc.close()

    docir = {
        "meta": {"title": "Export Test", "arxiv_id": "", "page_count": 2},
        "toc": [], "figures": [],
        "blocks": [
            {"id": "b1", "type": "paragraph", "text": "The encoder uses attention.",
             "page": 0, "bbox": _norm(70, 74, 320, 96), "section_path": "1"},
            {"id": "f1", "type": "formula", "text": "$E=mc^2$", "latex": "E=mc^2",
             "page": 0, "bbox": _norm(70, 286, 150, 308), "section_path": "2"},
            {"id": "b2", "type": "paragraph", "text": "Overlaps a table region.",
             "page": 0, "bbox": _norm(70, 486, 320, 508), "section_path": "2"},
            {"id": "b3", "type": "paragraph", "text": "Training took 3.5 days.",
             "page": 1, "bbox": _norm(70, 74, 320, 96), "section_path": "3"},
        ],
        "tables": [{"id": "t1", "page": 0, "bbox": _norm(60, 480, 340, 520),
                    "markdown": "|a|b|", "headers": ["a", "b"], "rows": [["1", "2"]]}],
    }
    (DOCIR_DIR / f"{PID}.json").write_text(json.dumps(docir), encoding="utf-8")
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO papers(id,title,file_path,docir_path,file_hash,"
                  "created_at) VALUES(?,?,?,?,?,?)",
                  (PID, "Export Test", str(pdf), str(DOCIR_DIR / f"{PID}.json"),
                   f"hash_{PID}", 0.0))
        for bid, txt in (("b1", "编码器使用注意力机制。"), ("b2", "与表格区域重叠的段落。"),
                         ("f1", "公式不应被替换。"), ("b3", "训练耗时 3.5 天。")):
            c.execute("INSERT INTO translations(paper_id,block_id,lang,text,src_hash,format_ok,"
                      "created_at) VALUES(?,?,?,?,?,?,?) "
                      "ON CONFLICT(paper_id,block_id,lang) DO UPDATE SET text=excluded.text",
                      (PID, bid, "zh|academic", txt, "h", 1, 0.0))
    yield


@pytest.fixture()
def _clean_exports():
    for f in (DATA_DIR / "exports").glob(f"{PID}.*"):
        f.unlink(missing_ok=True)
    with db.tx() as c:
        c.execute("DELETE FROM translation_exports WHERE paper_id=?", (PID,))
    yield


def test_overlay_preserves_layout_and_formula(_clean_exports):
    out = pdf_export.export_pdf(PID, target_lang="zh", mode="academic", layout="overlay")
    doc = fitz.open(out["path"])
    assert doc.page_count == 2, "overlay 页数必须与原文一致"
    p0_text = doc[0].get_text()
    assert "编码器使用注意力机制" in p0_text, "译文必须可检索（字体可嵌入抽取）"
    assert "attention" not in p0_text, "原文该块文本应被 redaction 移除"
    assert "$E=mc^2$" in p0_text, "公式块永不被替换"
    assert "与表格区域重叠" not in p0_text, "与表格 bbox 重叠（IoU>0.3）的块跳过替换"
    assert "表格区域" in p0_text or "Overlaps a table" in p0_text, "跳过块原文保留"
    assert "机器翻译草稿" in (doc.metadata or {}).get("title", "")
    doc.close()
    row = db.query("SELECT * FROM translation_exports WHERE paper_id=? ORDER BY created_at DESC",
                   (PID,))[0]
    assert row["fmt"] == "pdf" and row["layout"] == "overlay" and row["pages"] == 2


def test_bilingual_doubles_pages(_clean_exports):
    out = pdf_export.export_pdf(PID, target_lang="zh", mode="academic", layout="bilingual")
    doc = fitz.open(out["path"])
    assert doc.page_count == 4, "双语版式：每原页后插一页译文页"
    assert "编码器使用注意力机制" in doc[1].get_text()
    assert "attention" in doc[0].get_text(), "原页保持原样"
    doc.close()


def test_no_translations_guard(_clean_exports):
    with db.tx() as c:
        c.execute("DELETE FROM translations WHERE paper_id=?", (PID,))
    with pytest.raises(LookupError):
        pdf_export.export_pdf(PID, target_lang="en", mode="academic")
    # 恢复译文供后续用例
    with db.tx() as c:
        for bid, txt in (("b1", "编码器使用注意力机制。"), ("b3", "训练耗时 3.5 天。")):
            c.execute("INSERT INTO translations(paper_id,block_id,lang,text,src_hash,format_ok,"
                      "created_at) VALUES(?,?,?,?,?,?,?)",
                      (PID, bid, "zh|academic", txt, "h", 1, 0.0))


def test_md_html_export(_clean_exports):
    out = pdf_export.export_doc(PID, fmt="md", target_lang="zh", mode="academic")
    md = Path(out["path"]).read_text(encoding="utf-8")
    assert "编码器使用注意力机制" in md and "$$E=mc^2$$" in md, "公式以 LaTeX 保留"
    out2 = pdf_export.export_doc(PID, fmt="html", target_lang="zh", mode="academic",
                                 bilingual_view=True)
    html = Path(out2["path"]).read_text(encoding="utf-8")
    assert "attention" in html and "编码器使用注意力机制" in html, "双语对照：原文+译文都在"


def test_export_endpoint(_clean_exports):
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)
    r = client.get(f"/api/papers/{PID}/export?fmt=pdf&layout=overlay")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/pdf")
    assert int(r.headers.get("x-export-pages", 0)) == 2
    r2 = client.get(f"/api/papers/{PID}/export?fmt=md")
    assert r2.status_code == 200 and "编码器" in r2.text
    r3 = client.get("/api/papers/none_x/export?fmt=pdf")
    assert r3.status_code in (400, 404)
    r4 = client.get(f"/api/papers/{PID}/export?fmt=docx")
    assert r4.status_code == 400
