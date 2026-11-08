"""测试夹具：用 PyMuPDF 生成合成双栏论文 PDF（含标题/摘要/事实句/参考文献）。"""
from __future__ import annotations

from pathlib import Path

import fitz


def make_paper_pdf(path: Path, title: str, body_facts: list[str], refs: list[str],
                   arxiv_line: str = "arXiv:1706.03762v7") -> None:
    doc = fitz.open()
    W, H = 595, 842  # A4 pt

    # --- 第 1 页：标题 + 摘要 ---
    page = doc.new_page(width=W, height=H)
    y = 60
    page.insert_text((72, y), title, fontsize=14, fontname="helv")
    y += 24
    page.insert_text((72, y), "Anna Author, Bob Chen, Carol Li", fontsize=9)
    y += 18
    page.insert_text((72, y), arxiv_line, fontsize=8)
    y += 22
    page.insert_text((72, y), "Abstract", fontsize=11, fontname="hebo")
    y += 16
    page.insert_textbox(fitz.Rect(72, y, W - 72, y + 80),
                        "We propose the Transformer, the first sequence model based solely on attention. "
                        "It achieves state-of-the-art results on machine translation.",
                        fontsize=9)
    y += 90
    page.insert_text((72, y), "1 Introduction", fontsize=11, fontname="hebo")
    y += 16
    page.insert_textbox(fitz.Rect(72, y, W - 72, y + 120),
                        "Recurrent neural networks have long dominated sequence modeling. "
                        "We replace recurrence entirely with self-attention.",
                        fontsize=9)

    # --- 第 2 页：实验事实（双栏布局） ---
    page = doc.new_page(width=W, height=H)
    page.insert_text((72, 60), "2 Experiments", fontsize=11, fontname="hebo")
    col_l = fitz.Rect(72, 80, W / 2 - 10, H - 80)
    col_r = fitz.Rect(W / 2 + 10, 80, W - 72, H - 80)
    page.insert_textbox(col_l, body_facts[0] if body_facts else "placeholder.", fontsize=9)
    if len(body_facts) > 1:
        page.insert_textbox(col_r, body_facts[1], fontsize=9)

    # --- 第 3 页：References ---
    page = doc.new_page(width=W, height=H)
    page.insert_text((72, 60), "References", fontsize=11, fontname="hebo")
    y = 84
    for i, r in enumerate(refs, 1):
        page.insert_textbox(fitz.Rect(72, y, W - 72, y + 60), f"[{i}] {r}", fontsize=8)
        y += 34

    doc.save(str(path))
    doc.close()


def make_scanned_pdf(path: Path, title: str, body_facts: list[str],
                     refs: list[str] | None = None) -> None:
    """扫描件 fixture：矢量文本页渲染成位图重灌 → 无文本层（触发扫描件路由）。"""
    tmp = Path(str(path) + ".src.pdf")
    make_paper_pdf(tmp, title, body_facts=body_facts, refs=refs or ["Sample Ref. 2024."])
    src = fitz.open(tmp)
    dst = fitz.open()
    for page in src:
        pix = page.get_pixmap(dpi=144)
        npage = dst.new_page(width=page.rect.width, height=page.rect.height)
        npage.insert_image(npage.rect, pixmap=pix)
    dst.save(str(path))
    dst.close()
    src.close()
    tmp.unlink()


def make_long_pdf(path: Path, pages: int = 220, facts_per_page: int = 4) -> None:
    """长文档 fixture：pages 页矢量 PDF，带书签章节（每 20 页一章）→ 触发长文档路由。"""
    doc = fitz.open()
    W, H = 595, 842
    toc_entries = []
    for i in range(pages):
        page = doc.new_page(width=W, height=H)
        if i % 20 == 0:
            chap = f"Chapter {i // 20 + 1}: Section {i // 20 + 1}"
            page.insert_text((72, 60), chap, fontsize=12, fontname="hebo")
            toc_entries.append([1, chap, i + 1])   # get_toc 页码 1 基
            y = 84
        else:
            y = 60
        for k in range(facts_per_page):
            page.insert_textbox(fitz.Rect(72, y, W - 72, y + 130),
                                f"Page {i + 1} paragraph {k + 1}: the model was trained on "
                                f"{k + 1} GPUs and reaches {20 + k}.5 BLEU on translation.",
                                fontsize=9)
            y += 140
    doc.set_toc(toc_entries)
    doc.save(str(path))
    doc.close()
