"""解析路由与多通道引擎测试（密闭：屏蔽凭据，MINERU/VLM 关闭走确定性回退）。"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SCHOLAR_DATA_DIR", str(Path(__file__).parent / f"_prs_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_PROVIDER"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["MINIMAX_API_KEY"] = ""     # 本机全局 env 有 minimax key，必须显式清空（密闭性）
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["MINERU_ENABLED"] = "off"       # 密闭环境固定走 PyMuPDF 主路，确定性断言
os.environ["EMBEDDING_BACKEND"] = "off"

from app.parsing import pipeline, quality
from app.parsing.mineru_engine import middle_to_docir
from app.parsing.router import parse_document, probe_pdf, route
from app.parsing.tables import html_table_to_rows, parse_md_table
from app.parsing.vlm_engine import markdown_to_docir
from tests.fixtures_pdf import make_long_pdf, make_paper_pdf, make_scanned_pdf

DATA = Path(os.environ["SCHOLAR_DATA_DIR"])


@pytest.fixture(scope="module")
def vector_pdf() -> Path:
    p = DATA / "vec.pdf"
    make_paper_pdf(p, "Attention Is All You Need",
                   body_facts=["The model was trained on 8 NVIDIA P100 GPUs for 3.5 days.",
                               "The base model reaches 27.3 BLEU on English-to-German."],
                   refs=["Vaswani et al. 2017. Attention Is All You Need. arXiv:1706.03762"])
    return p


@pytest.fixture(scope="module")
def scanned_pdf() -> Path:
    p = DATA / "scan.pdf"
    make_scanned_pdf(p, "Attention Is All You Need",
                     body_facts=["The model was trained on 8 NVIDIA P100 GPUs."])
    return p


@pytest.fixture(scope="module")
def long_pdf() -> Path:
    p = DATA / "book.pdf"
    make_long_pdf(p, pages=220)
    return p


# ---------- probe / route ----------

def test_probe_and_route_vector(vector_pdf):
    probe = probe_pdf(vector_pdf)
    assert probe["page_count"] == 3 and probe["has_text_layer"]
    assert probe["avg_chars"] > 100 and probe["image_ratio"] < 0.1
    d = route(probe)
    # 稀疏 fixture（~270 字符/页）正确落入灰区；密集文档由 long fixture 断言 native
    assert d["mode"] == "gray" and d["long"] is False
    assert d["engine"] == "pymupdf"   # MINERU_ENABLED=off


def test_probe_and_route_scanned(scanned_pdf):
    probe = probe_pdf(scanned_pdf)
    assert not probe["has_text_layer"] and probe["image_ratio"] > 0.6
    d = route(probe)
    assert d["mode"] == "scanned" and d["long"] is False


def test_probe_and_route_long(long_pdf):
    probe = probe_pdf(long_pdf)
    assert probe["page_count"] == 220 and probe["has_text_layer"]
    d = route(probe)
    assert d["long"] is True and d["mode"] == "native"


# ---------- PyMuPDF 兜底引擎：全流程 + 页范围 ----------

def test_parse_document_vector_fallback(vector_pdf):
    docir = parse_document(vector_pdf)
    m = docir["meta"]
    assert m["parse_engine"] == "pymupdf" and m["route_mode"] == "gray"
    assert m["long_doc"] is False and "quality" in m and "parse_quality" in m
    assert m["quality"]["table_count"] >= 0
    assert docir["toc"] and docir["references"]
    assert all(b.get("bbox") and len(b["bbox"]) == 4 for b in docir["blocks"])


def test_parse_pdf_pages_range(vector_pdf):
    docir = pipeline.parse_pdf(vector_pdf, pages=(1, 2))
    assert docir["blocks"] and all(b["page"] == 1 for b in docir["blocks"])


def test_parse_document_scanned_honest_fallback(scanned_pdf):
    """无 MinerU/无 VLM key：扫描件 → PyMuPDF 空层 + 诚实标注，不崩不冒充。"""
    docir = parse_document(scanned_pdf)
    m = docir["meta"]
    assert m["parse_engine"] == "pymupdf" and m["scanned"] is True
    assert len(m["scanned_pages"]) == 3
    assert m.get("scanned_note") and "OCR" in m["scanned_note"]
    assert m["quality"]["scanned_ratio"] == 1.0


def test_parse_document_long_streaming(long_pdf):
    """长文档：批级解析 + 大纲先出 + meta.long_doc；书签 toc 完整且 0 基。"""
    progress: list[tuple[float, str]] = []
    docir = parse_document(long_pdf, progress=lambda f, msg: progress.append((f, msg)),
                           paper_id="p_long_test")
    m = docir["meta"]
    assert m["long_doc"] is True and m["page_count"] == 220
    # 大纲先出：书签 11 章（0/20/…/200 页）全部解析完成
    assert len(docir["toc"]) == 11 and docir["toc"][0]["page"] == 0
    assert len(docir["blocks"]) > 100
    # 批级进度可见（220 页 / 50 = 5 批）
    msgs = [msg for _, msg in progress]
    assert any("批次 3/" in msg for msg in msgs) and any("已解析" in msg for msg in msgs)
    # 落盘文件存在（大纲先出 + 逐批重写）
    assert (DATA / "docir" / "p_long_test.json").exists()


# ---------- VLM markdown → DocIR ----------

def test_markdown_to_docir_mapping():
    md = (
        "# Attention Is All You Need\n"
        "We use multi-head attention with 8 heads.\n"
        "$$\n"
        "x = W_o \\cdot h\n"
        "$$\n"
        "| Model | BLEU |\n|---|---|\n| Base | 27.3 |\n| Big | 29.3 |\n"
        "[图] Figure 1: architecture\n"
    )
    # fixture 中 $$..$$ 常被 LLM 输出为多行 → 先归一为单行形式再喂（引擎调用前不假设）
    md_single = md.replace("$$\nx = W_o \\cdot h\n$$", "$$x = W_o \\cdot h$$")
    out = markdown_to_docir(md_single, 3)
    types = [b["type"] for b in out["blocks"]]
    assert "heading" in types and "formula" in types
    formula = next(b for b in out["blocks"] if b["type"] == "formula")
    assert formula["latex"] == "x = W_o \\cdot h" and formula["page"] == 3
    assert len(out["tables"]) == 1
    assert out["tables"][0]["headers"] == ["Model", "BLEU"]
    assert out["tables"][0]["rows"] == [["Base", "27.3"], ["Big", "29.3"]]
    assert out["figures"] and out["figures"][0]["caption"].startswith("Figure 1")
    assert all(b["bbox"] == [0.0, 0.0, 1.0, 1.0] for b in out["blocks"])  # VLM 无坐标 → 全页占位


# ---------- MinerU middle.json → DocIR（纯映射单测，无需真实权重） ----------

def _fake_middle() -> dict:
    """按 MinerU 2.x pipeline 后端 middle.json 结构构造样例（bbox 单位 pt，左上原点）。"""
    return {"pdf_info": [
        {"page_idx": 0, "page_size": [595.0, 842.0], "preproc_blocks": [
            {"type": "title", "bbox": [72.0, 60.0, 400.0, 80.0],
             "lines": [{"spans": [{"type": "text", "content": "Attention Is All You Need"}]}]},
            {"type": "text", "bbox": [72.0, 100.0, 520.0, 160.0],
             "lines": [{"spans": [{"type": "text", "content": "Trained on 8 NVIDIA P100 GPUs."},
                                  {"type": "inline_equation", "content": "",
                                   "latex": "\\alpha"}]}]},
            {"type": "equation", "bbox": [200.0, 200.0, 395.0, 240.0],
             "lines": [{"spans": [{"type": "interline_equation",
                                   "content": "", "latex": "\\mathrm{Attention}(Q,K,V)"}]}]},
            {"type": "table", "bbox": [72.0, 300.0, 520.0, 420.0],
             "blocks": [{"type": "table_body", "lines": [{"spans": [{"type": "table", "html":
                "<table><tr><td>Model</td><td>BLEU</td></tr>"
                "<tr><td>Base</td><td>27.3</td></tr></table>"}]}]},
                        {"type": "table_caption", "lines": [{"spans": [{"type": "text", "content":
                            "Table 1: BLEU"}]}]}]},
            {"type": "image", "bbox": [300.0, 460.0, 520.0, 600.0],
             "blocks": [{"type": "image_body", "lines": [{"spans": [{"type": "image",
                "image_path": "images/f1.jpg"}]}]},
                        {"type": "image_caption", "lines": [{"spans": [{"type": "text", "content":
                            "Figure 1: arch"}]}]}]},
        ]}]}


def test_middle_to_docir_mapping(vector_pdf):
    docir = middle_to_docir(_fake_middle(), vector_pdf, page_offset=0)
    assert docir["meta"]["page_count"] == 1
    types = [b["type"] for b in docir["blocks"]]
    assert types == ["heading", "paragraph", "formula"]
    # bbox 归一化（pt/页宽高）：72/595=0.121，60/842=0.0713
    assert docir["blocks"][0]["bbox"][0] == pytest.approx(72 / 595, abs=1e-3)
    assert docir["blocks"][0]["bbox"][1] == pytest.approx(60 / 842, abs=1e-3)
    # 行内公式内嵌 $..$
    para = docir["blocks"][1]["text"]
    assert "8 NVIDIA P100 GPUs" in para and "$\\alpha$" in para
    # 行间公式 → formula 块，text=latex 供 RAG/KaTeX 双用
    assert docir["blocks"][2]["latex"] == "\\mathrm{Attention}(Q,K,V)"
    # 表格：html → markdown + headers/rows + caption
    t = docir["tables"][0]
    assert t["headers"] == ["Model", "BLEU"] and t["rows"] == [["Base", "27.3"]]
    assert "| BLEU |" in t["markdown"] and t["title"].startswith("Table 1")
    # 图片：caption + image_path（Windows 下 Path 归一化为反斜杠，用 Path 语义比较）
    f = docir["figures"][0]
    assert f["caption"] == "Figure 1: arch"
    assert Path(f["image_path"]) == Path("images") / "f1.jpg"
    # 页码偏移（长文档批次）
    docir2 = middle_to_docir(_fake_middle(), vector_pdf, page_offset=50)
    assert docir2["blocks"][0]["page"] == 50


# ---------- 表格工具 / 质量指标 ----------

def test_tables_utils():
    assert parse_md_table("| A | B |\n|---|---|\n| 1 | 2 |") == (["A", "B"], [["1", "2"]])
    h, r = html_table_to_rows("<table><tr><td>A</td><td>B</td></tr>"
                              "<tr><td>1</td><td>2</td></tr></table>")
    assert h == ["A", "B"] and r == [["1", "2"]]
    assert html_table_to_rows("<p>no table</p>") == ([], [])


def test_quality_metrics(vector_pdf):
    docir = parse_document(vector_pdf)
    q = docir["meta"]["quality"]
    assert q["text_coverage"] == 1.0
    assert q["toc_depth"] >= 1
    assert q["reference_parse_rate"] is not None
    assert 0.0 <= docir["meta"]["parse_quality"] <= 1.0
    # 空文档不崩
    q2 = quality.compute({"blocks": [], "toc": [], "tables": [], "references": [],
                          "meta": {"page_count": 0}}, None)
    assert q2["overall"] >= 0.0
