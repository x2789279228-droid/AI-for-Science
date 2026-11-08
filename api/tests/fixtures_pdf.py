"""测试夹具：用 PyMuPDF 生成合成双栏论文 PDF（含标题/摘要/事实句/参考文献）。

模板文本按 title 派生（作者行/摘要/引言注入标题词）：不同题目的合成论文不再共享
大段固定文本——否则 5 字符 shingle MinHash 会被模板 boilerplate 系统性抬高
（跨题目夹具实测 minhash 0.86~1.0，制造假同族）。同题不同版本（真对）仍共享
派生文本，保持高相似——正是判重目标。
"""
from __future__ import annotations

import re
from pathlib import Path

import fitz


def _derived_texts(title: str, body_facts: list[str]) -> tuple[str, str, str]:
    """(title+facts) → (作者行, 摘要, 引言)：身份派生文本，跨论文不共享。
    派生含 facts——同题不同文的合成论文（硬负例形态）在摘要/引言/作者层也不同；
    句式骨架按身份哈希选变体，避免跨题共享固定句子（旧模板的假同族根因）。"""
    import hashlib

    ident = f"{title}|{'|'.join(body_facts)}"
    key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", ident.lower())[:24] or "paper"
    h = int(hashlib.sha1(ident.encode("utf-8")).hexdigest(), 16)
    # 姓名含身份哈希：同题不同文的合成论文作者团队也不同（难负例 c 的判别前提）
    lead = f"{key[:8]}{h % 89:02d}".title() or "Anon"
    verbs = ["study", "present", "introduce", "describe"]
    evals = ["controlled comparisons", "systematic ablations", "large-scale benchmarks",
             "cross-dataset evaluations"]
    gains = ["consistent gains across settings", "robust improvements over baselines",
             "state-of-the-art results on the main metric", "reliable improvements"]
    authors = f"{lead} Author, {lead[:6]} Researcher, Third Author"
    abstract = (f"We {verbs[h % 4]} {title}. Our method targets the core problem named in "
                f"the title and reports {evals[(h >> 8) % 4]} on the {key[:10]} benchmark, "
                f"showing {gains[(h >> 16) % 4]}.")
    intro = (f"Introduction to {title}. Prior work leaves the main question open; we "
             f"formalize the problem and validate the approach on {key[:10]} tasks "
             f"under {(h >> 24) % 4 + 2} evaluation protocols.")
    return authors, abstract, intro


def make_paper_pdf(path: Path, title: str, body_facts: list[str], refs: list[str],
                   arxiv_line: str = "arXiv:1706.03762v7") -> None:
    doc = fitz.open()
    W, H = 595, 842  # A4 pt
    authors, abstract, intro = _derived_texts(title, body_facts)

    # --- 第 1 页：标题 + 摘要 ---
    page = doc.new_page(width=W, height=H)
    y = 60
    page.insert_text((72, y), title, fontsize=14, fontname="helv")
    y += 24
    page.insert_text((72, y), authors, fontsize=9)
    y += 18
    page.insert_text((72, y), arxiv_line, fontsize=8)
    y += 22
    page.insert_text((72, y), "Abstract", fontsize=11, fontname="hebo")
    y += 16
    page.insert_textbox(fitz.Rect(72, y, W - 72, y + 80), abstract, fontsize=9)
    y += 90
    page.insert_text((72, y), "1 Introduction", fontsize=11, fontname="hebo")
    y += 16
    page.insert_textbox(fitz.Rect(72, y, W - 72, y + 120), intro, fontsize=9)

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
