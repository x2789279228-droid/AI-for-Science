"""真实论文集成测试（官方用例：1706.03762v7 矢量双栏 PDF）。

PDF 不存在时跳过；存在则验证：标题/标题树/文献/表格提取 + 事实检索。
下载：https://arxiv.org/pdf/1706.03762v7 → 放到 downloads/1706.03762/
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ["MINERU_ENABLED"] = "off"  # 密闭测试禁用 MinerU 子进程（走 PyMuPDF 基线）

from app.parsing.pipeline import parse_pdf
from app.rag import indexing
from app.rag.chunking import chunk_docir

PDF = Path(os.environ.get("SCHOLAR_REAL_PDF",
                           "D:/传智杯/downloads/1706.03762/1706.03762v7.pdf"))

pytestmark = pytest.mark.skipif(not PDF.exists(), reason=f"未找到真实论文 {PDF}")


def test_real_paper_parse_and_retrieve():
    docir = parse_pdf(PDF)
    assert docir["meta"]["title"] == "Attention Is All You Need"
    assert docir["meta"]["page_count"] == 15
    assert docir["meta"]["arxiv_id"] == "1706.03762"
    assert len(docir["toc"]) >= 15          # 真实标题树
    assert len(docir["references"]) >= 30   # References 逐条解析
    assert docir["tables"], "应提取出表格"
    assert docir["figures"], "应提取出图"

    chunks = chunk_docir(docir)
    assert 40 <= len(chunks) <= 150
    indexing.index_chunks("p_realtest", chunks)

    # 事实级检索（方案验收点：GPU / BLEU）
    hits = indexing.fts_search(["p_realtest"], "8 NVIDIA P100 GPUs", top_k=3)
    assert hits and "P100" in hits[0]["text"]
    hits = indexing.fts_search(["p_realtest"], "BLEU 41.0 English-to-French", top_k=3)
    assert hits and "41" in hits[0]["text"]
