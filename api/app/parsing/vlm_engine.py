"""扫描件 VLM 引擎：Qwen-VL 整页结构化 → DocIR（方案 docs/01 §4.2 扫描件通道）。

- parse_scanned：扫描件全文档逐页 VLM（有文本层的页跳过，节约调用）
- rescue_blank_pages：灰区文档的空白页逐页救援（评分表口径"只对失败页调用"）
- 诚实标注：VLM 页块 bbox 为全页占位（无版面坐标），meta.scanned_pages 留痕，
  前端显示"扫描件 · 识别结果可能不准"徽章；调用失败该页退回空层不冒充。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from ..llm import vision
from ._types import CancelCheck, ParseCancelled, ProgressCb
from .pipeline import (
    _mark_appendix,
    _parse_references,
    backfill_sections,
)
from .tables import parse_md_table

PAGE_PROMPT = (
    "识别这张学术文档页面图，输出 Markdown（禁止编造页面中不存在的内容）：\n"
    "1. 标题行用 #/##/### 标注层级；\n"
    "2. 数学公式用 LaTeX：独立公式块用 $$...$$ 单独成行，行内公式用 $...$；\n"
    "3. 表格用 GFM 管道表格输出；图片只写一行图注：`[图] 图注文字`；\n"
    "4. 正文段落逐字保持原文，不要总结、不要翻译、不要添加页眉页脚水印；\n"
    "5. 每个标题/段落/公式块的第一行行首输出版面框标记 <<bbox:x0,y0,x1,y1>>，"
    "四个 0-1000 整数（页面归一化坐标，左上为原点）；推不出时省略该标记，禁止编造坐标。\n"
    "只输出 Markdown 本身，不要解释。"
)
PROMPT_VERSION = "v2-grounding"   # v1：无坐标；v2：块级 bbox 标记（尽力而为，解析侧诚实回退）

_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_FORMULA_LINE = re.compile(r"^\$\$(.+?)\$\$\s*$")
_FIG_CAPTION = re.compile(r"^\[图\]\s*(.+)$")
_BBOX_TAG = re.compile(r"^<<bbox:(\d{1,4}),(\d{1,4}),(\d{1,4}),(\d{1,4})>>\s*")
_RENDER_DPI = 150


def _parse_bbox_prefix(line: str) -> tuple[str, list[float] | None]:
    """块首行 `<<bbox:x0,y0,x1,y1>>` 标记 → (清理后文本, 归一化 bbox 或 None)。
    数值 clamp 到 0-1；x1<=x0 / y1<=y0 等非法形状回退 None（诚实占位）。"""
    m = _BBOX_TAG.match(line.strip())
    if not m:
        return line, None
    clean = _BBOX_TAG.sub("", line.strip(), count=1)
    try:
        x0, y0, x1, y1 = (max(0, min(1000, int(g))) / 1000 for g in m.groups())
    except ValueError:
        return clean, None
    if x1 <= x0 or y1 <= y0:
        return clean, None
    return clean, [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


def available() -> bool:
    return vision.available()


def render_page_png(doc: fitz.Document, pageno: int) -> bytes:
    page = doc[pageno]
    pix = page.get_pixmap(dpi=_RENDER_DPI)
    return pix.tobytes("png")


def markdown_to_docir(md: str, pageno: int, *, bid_start: int = 0,
                      t_start: int = 0, f_start: int = 0) -> dict:
    """VLM 输出的整页 Markdown → 该页 DocIR 片段 {blocks, tables, figures}。

    公式块：$$...$$ → type=formula（text=latex 源码，供 RAG 与 KaTeX 渲染）。
    bbox：PROMPT v2 要求块首行 <<bbox:x0,y0,x1,y1>>（0-1000 归一化）——解析成功即用
    实值；缺失/非法 → 全页占位 [0,0,1,1] + bbox_estimated=True（诚实降级，页级高亮仍可用）。
    """
    blocks: list[dict] = []
    tables: list[dict] = []
    figures: list[dict] = []
    bid, tid, fid = bid_start, t_start, f_start
    lines = (md or "").splitlines()
    i = 0
    para: list[str] = []
    para_bbox: list[float] | None = None

    def flush_para() -> None:
        nonlocal bid, para, para_bbox
        text = "\n".join(para).strip()
        bbox = para_bbox
        para, para_bbox = [], None
        if not text:
            return
        bid += 1
        entry = {"id": f"b{bid}", "type": "paragraph", "text": text,
                 "latex": None, "page": pageno,
                 "bbox": bbox or [0.0, 0.0, 1.0, 1.0], "section_path": ""}
        if bbox is None:
            entry["bbox_estimated"] = True
        blocks.append(entry)

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|"):                     # GFM 表格
            flush_para()
            tbl: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl.append(lines[i])
                i += 1
            md_tbl = "\n".join(tbl)
            headers, rows = parse_md_table(md_tbl)
            tid += 1
            tables.append({"id": f"t{tid}", "page": pageno, "bbox": [0.0, 0.0, 1.0, 1.0],
                           "markdown": md_tbl,
                           **({"headers": headers, "rows": rows} if headers else {})})
            continue
        clean, bbox = _parse_bbox_prefix(line)
        fm = _HEADING.match(clean)
        if fm:
            flush_para()
            bid += 1
            entry = {"id": f"b{bid}", "type": "heading",
                     "text": fm.group(2).strip(), "latex": None, "page": pageno,
                     "bbox": bbox or [0.0, 0.0, 1.0, 1.0], "section_path": "",
                     "level": min(len(fm.group(1)), 3)}
            if bbox is None:
                entry["bbox_estimated"] = True
            blocks.append(entry)
            i += 1
            continue
        fe = _FORMULA_LINE.match(clean.strip())
        if fe:
            flush_para()
            bid += 1
            latex = fe.group(1).strip()
            entry = {"id": f"b{bid}", "type": "formula", "text": latex,
                     "latex": latex, "page": pageno,
                     "bbox": bbox or [0.0, 0.0, 1.0, 1.0], "section_path": ""}
            if bbox is None:
                entry["bbox_estimated"] = True
            blocks.append(entry)
            i += 1
            continue
        fc = _FIG_CAPTION.match(clean.strip())
        if fc:
            flush_para()
            fid += 1
            figures.append({"id": f"f{fid}", "page": pageno,
                            "caption": fc.group(1)[:200], "bbox": [0.0, 0.0, 1.0, 1.0]})
            i += 1
            continue
        if clean.strip():
            if not para and bbox is not None:   # bbox 标记只在块首行生效
                para_bbox = bbox
            para.append(clean.rstrip())
        else:
            flush_para()
        i += 1
    flush_para()
    return {"page": pageno, "blocks": blocks, "tables": tables, "figures": figures}


def _assemble(docir: dict, parts: list[dict], pdf_path: Path, page_count: int) -> dict:
    """页面片段 → 完整 DocIR（id 重排、toc/sections 回填、references 解析、meta）。"""
    blocks: list[dict] = []
    tables: list[dict] = []
    figures: list[dict] = []
    for part in sorted(parts, key=lambda p: p["page"]):
        for b in part["blocks"]:
            b["id"] = f"b{len(blocks) + 1}"
            blocks.append(b)
        for t in part["tables"]:
            t["id"] = f"t{len(tables) + 1}"
            tables.append(t)
        for f in part["figures"]:
            f["id"] = f"f{len(figures) + 1}"
            figures.append(f)

    toc: list[dict] = []
    seen: set[str] = set()
    for b in blocks:
        if b["type"] == "heading" and b["text"] not in seen:
            seen.add(b["text"])
            toc.append({"level": b.get("level", 2), "title": b["text"], "page": b["page"]})

    blocks_by_page: dict[int, list[dict]] = {}
    for b in blocks:
        blocks_by_page.setdefault(b["page"], []).append(b)
    backfill_sections(blocks, toc, blocks_by_page)

    # References：_find_refs_start 定位（防标题与首条目粘连的形态）→ _ref_block_texts
    # （Appendix 边界截断 + 空行拼接：author-year 风格块边界=条目边界）
    from .pipeline import _find_refs_start, _ref_block_texts
    ri = _find_refs_start(blocks)
    ref_parts: list[str] = _ref_block_texts(blocks, ri) if ri >= 0 else []

    title = next((t["title"] for t in toc if t["level"] == 1), "") \
        or next((b["text"] for b in blocks if b["type"] == "heading"), "") or pdf_path.stem
    docir.update({
        "paper_id": pdf_path.stem,
        "meta": {"title": title, "authors": [], "arxiv_id": "", "pub_date": "",
                 "venue": "", "page_count": page_count, "file": pdf_path.name},
        "toc": toc,
        "blocks": blocks,
        "tables": tables,
        "figures": figures,
        "references": _parse_references("\n\n".join(ref_parts)) if ref_parts else [],
    })
    _mark_appendix(toc, docir["meta"], blocks=blocks)
    return docir


def parse_scanned(pdf_path: str | Path, progress: ProgressCb | None = None,
                  cancel_check: CancelCheck | None = None) -> dict:
    """扫描件全文档：逐页 VLM（跳过有文本层的页，只对失败页调用）。"""
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)
    n = doc.page_count
    char_counts = [len(p.get_text("text").strip()) for p in doc]
    scanned_pages = [i for i, c in enumerate(char_counts) if c < 50]
    parts: list[dict] = []
    done = 0
    try:
        for pageno in scanned_pages:
            if cancel_check is not None and cancel_check():
                raise ParseCancelled(f"VLM 逐页识别已取消（第 {pageno} 页边界）")
            md = vision.vlm_chat(PAGE_PROMPT, [render_page_png(doc, pageno)],
                                 module="vlm_parse", audit_prompt=f"scanned p{pageno + 1}"
                                 f" prompt={PROMPT_VERSION}", max_tokens=3600)
            if md:
                parts.append(markdown_to_docir(md, pageno))
            else:
                parts.append({"blocks": [], "tables": [], "figures": [], "page": pageno})
            done += 1
            if progress:
                progress(0.05 + 0.75 * done / max(len(scanned_pages), 1),
                         f"VLM 识别第 {pageno + 1}/{n} 页")
    finally:
        doc.close()
    docir: dict[str, Any] = {}
    _assemble(docir, parts, pdf_path, n)
    docir["meta"]["scanned_pages"] = scanned_pages
    docir["meta"]["vlm_failed_pages"] = [p for p, part in zip(scanned_pages, parts)
                                         if not part["blocks"] and not part["tables"]]
    docir["meta"]["vlm_prompt"] = PROMPT_VERSION
    docir["meta"]["vlm_bbox_mode"] = "grounding" if any(
        not b.get("bbox_estimated") for b in docir.get("blocks", [])) else "placeholder"
    if not docir.get("blocks") and not docir.get("tables"):
        docir["meta"]["parse_empty"] = True   # 全页识别失败：诚实标注，功能上无产出
    return docir


def rescue_blank_pages(pdf_path: str | Path, docir: dict,
                       progress: ProgressCb | None = None,
                       cancel_check: CancelCheck | None = None) -> dict:
    """灰区文档救援：对 PyMuPDF 解析后仍近乎空白的页逐页 VLM，块并入 DocIR。"""
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)
    try:
        char_counts = [len(p.get_text("text").strip()) for p in doc]
    finally:
        doc.close()
    have = {b["page"] for b in docir.get("blocks", [])}
    blank = [p for p, c in enumerate(char_counts) if c < 50 and p not in have
             and p < docir["meta"]["page_count"]]
    if not blank:
        return docir

    rescued: list[dict] = []
    for i, pageno in enumerate(blank):
        if cancel_check is not None and cancel_check():
            raise ParseCancelled(f"VLM 逐页救援已取消（第 {pageno} 页边界）")
        d = fitz.open(pdf_path)
        try:
            png = render_page_png(d, pageno)
        finally:
            d.close()
        md = vision.vlm_chat(PAGE_PROMPT, [png], module="vlm_rescue",
                             audit_prompt=f"rescue p{pageno + 1}")
        if md:
            rescued.append(markdown_to_docir(md, pageno))
        if progress:
            progress(0.75 + 0.08 * (i + 1) / len(blank),
                     f"VLM 救援第 {pageno + 1} 页（{i + 1}/{len(blank)}）")
    if not rescued:
        return docir

    for part in rescued:
        docir["blocks"].extend(part["blocks"])
        docir["tables"].extend(part["tables"])
        docir["figures"].extend(part["figures"])
    docir["meta"]["rescued_pages"] = sorted(p["page"] for p in rescued)
    sp = docir["meta"].setdefault("scanned_pages", [])
    sp.extend(p for p in docir["meta"]["rescued_pages"] if p not in sp)
    return docir
