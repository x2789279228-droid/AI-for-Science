"""DocIR 解析管道（模块一·快速路径）。

PyMuPDF 提取 → 双栏阅读顺序重排（x 坐标聚类）→ 标题树（字号层级）
→ 表格（find_tables）→ 图片+caption 关联 → References 锚点切段逐条解析。
输出统一 DocIR JSON（方案 §4.1），溯源锚点 (page, bbox) 一等公民。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

# ---------- 工具 ----------

_REF_HEAD = re.compile(r"^\s*(references|bibliography|文献)\s*$", re.I)
_REF_ITEM = re.compile(r"^\[(\d{1,3})\]\s*(.*)$")
_ARXIV_ID = re.compile(r"arxiv[:\s]*(\d{4}\.\d{4,5})(v\d+)?", re.I)
_DOI = re.compile(r"10\.\d{4,9}/[\w.\-/:]+", re.I)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _has_text_layer(doc: fitz.Document) -> bool:
    """文本层检测：前 5 页平均字符密度过低 → 视为扫描件（走困难路径，MVP 先标记）。"""
    if doc.page_count == 0:
        return False
    sample = min(5, doc.page_count)
    chars = sum(len(doc[i].get_text("text").strip()) for i in range(sample))
    return chars / sample >= 80  # 每页至少 ~80 字符


# ---------- 阅读顺序（双栏重排） ----------

def _reading_order(blocks: list[dict]) -> list[dict]:
    """阅读顺序重排 v2（归一化 bbox，页宽=1）。

    规则：跨中线的宽块视为分隔符（标题/通栏摘要/表格），flush 已积攒的
    左右栏队列后插入；非跨栏块按中心 x 分入左/右队列，flush 时左列全出
    再出右列（双栏先左后右）。竖排页边横幅（arXiv 侧边条）为 furniture 置底。
    """
    if not blocks:
        return blocks
    mid = 0.5
    def crosses(b: dict) -> bool:
        x0, _, x1, _ = b["bbox"]
        return x0 < mid - 0.04 and x1 > mid + 0.04
    furniture = [b for b in blocks if (b["bbox"][2] - b["bbox"][0]) < 0.08
                 and (b["bbox"][3] - b["bbox"][1]) > 0.25]
    body = [b for b in blocks if b not in furniture]

    left: list[dict] = []
    right: list[dict] = []
    out: list[dict] = []
    for b in sorted(body, key=lambda b: b["bbox"][1]):
        if crosses(b):
            out += sorted(left, key=lambda x: (x["bbox"][1], x["bbox"][0]))
            out += sorted(right, key=lambda x: (x["bbox"][1], x["bbox"][0]))
            left, right = [], []
            out.append(b)
        elif (b["bbox"][0] + b["bbox"][2]) / 2 < mid:
            left.append(b)
        else:
            right.append(b)
    out += sorted(left, key=lambda x: (x["bbox"][1], x["bbox"][0]))
    out += sorted(right, key=lambda x: (x["bbox"][1], x["bbox"][0]))
    out += sorted(furniture, key=lambda x: (x["bbox"][1], x["bbox"][0]))
    return out


# ---------- 标题层级 ----------

def _build_toc(doc: fitz.Document, blocks_by_page: dict[int, list[dict]]) -> list[dict]:
    """优先 PDF 书签；否则按字号层级推断标题块。"""
    toc = []
    try:
        for lvl, title, page in doc.get_toc():
            toc.append({"level": min(int(lvl), 3), "title": title.strip(), "page": max(int(page), 1)})
    except Exception:
        pass
    if toc:
        return toc
    sizes: list[float] = []
    for pageno, blocks in blocks_by_page.items():
        for b in blocks:
            for line in b.get("lines", []):
                for span in line["spans"]:
                    if len(span["text"].strip()) > 2:
                        sizes.append(round(span["size"], 1))
    if not sizes:
        return []
    sizes.sort(reverse=True)
    big = sizes[max(int(len(sizes) * 0.03), 0)]   # 前 3% 字号 → level1
    mid = sizes[max(int(len(sizes) * 0.10), 1)]   # 前 10% → level2
    seen: set[str] = set()
    for pageno in sorted(blocks_by_page):
        for b in blocks_by_page[pageno]:
            bsize = max((s["size"] for ln in b.get("lines", []) for s in ln["spans"]), default=0)
            text = b.get("text", "").strip()
            if not text or len(text) > 120:
                continue
            lvl = 1 if bsize >= big else 2 if bsize >= mid else 0
            if lvl and text not in seen and not text.endswith((".", "。", ";", "；")):
                seen.add(text)
                toc.append({"level": lvl, "title": text, "page": pageno})
    return toc


# ---------- References 解析 ----------

def _parse_references(ref_text: str) -> list[dict]:
    """逐行状态机：`[n]` 开新条目，后续折行并入当前条目。"""
    items: list[tuple[int, str]] = []
    cur_idx: int | None = None
    cur = ""
    for line in ref_text.split("\n"):
        line = line.strip()
        m = _REF_ITEM.match(line)
        if m:
            if cur_idx is not None:
                items.append((cur_idx, cur.strip()))
            cur_idx, cur = int(m.group(1)), m.group(2)
        elif line and cur_idx is not None:
            cur += " " + line
    if cur_idx is not None:
        items.append((cur_idx, cur.strip()))
    if not items:  # 无 [n] 编号：按空行/长句切段
        for i, seg in enumerate([s.strip() for s in re.split(r"\n\s*\n", ref_text)
                                 if len(s.strip()) > 30], 1):
            items.append((i, re.sub(r"\s+", " ", seg)))

    out = []
    for idx, raw in items:
        raw = re.sub(r"\s+", " ", raw).strip()
        parts = raw.split(". ")
        m = _ARXIV_ID.search(raw)
        doi = _DOI.search(raw)
        out.append({"index": idx, "raw": raw[:500],
                    "title": parts[1].strip()[:200] if len(parts) >= 2 else "",
                    "arxiv_id": (m.group(1) + (m.group(2) or "")) if m else "",
                    "doi": doi.group(0) if doi else ""})
    return out


# ---------- 主流程 ----------

def parse_pdf(pdf_path: str | Path, progress=None) -> dict:
    """PDF → DocIR JSON。progress: 可选回调 (frac 0~1, msg)。"""
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)

    def report(frac: float, msg: str) -> None:
        if progress:
            progress(frac, msg)

    if not _has_text_layer(doc):
        report(0.0, "no_text_layer")   # 扫描件标记：后续由 Qwen-VL 困难路径接管（MVP 暂降级提示）

    meta: dict[str, Any] = {}
    blocks: list[dict] = []
    tables: list[dict] = []
    figures: list[dict] = []
    blocks_by_page: dict[int, list[dict]] = {}
    ref_start_page: int | None = None
    ref_text_parts: list[str] = []

    n = doc.page_count
    bid = 0
    for pageno in range(n):
        page = doc[pageno]
        pw, ph = page.rect.width, page.rect.height
        raw = page.get_text("dict")
        pblocks = []
        for blk in raw.get("blocks", []):
            if blk["type"] != 0:  # 0=text, 1=image
                continue
            x0, y0, x1, y1 = blk["bbox"]
            lines = blk.get("lines", [])
            text = "\n".join("".join(s["text"] for s in ln["spans"]) for ln in lines).strip()
            if not text:
                continue
            b = {"bbox": [round(x0 / pw, 4), round(y0 / ph, 4), round(x1 / pw, 4), round(y1 / ph, 4)],
                 "lines": lines, "text": text,
                 "size": max((s["size"] for ln in lines for s in ln["spans"]), default=10.0)}
            pblocks.append(b)
        pblocks = _reading_order(pblocks)
        blocks_by_page[pageno] = pblocks

        for b in pblocks:
            text = b["text"]
            bid += 1
            btype = "heading" if text in {t["title"] for t in []} else "paragraph"
            blocks.append({"id": f"b{bid}", "type": btype, "text": text, "latex": None,
                           "page": pageno, "bbox": b["bbox"], "section_path": ""})
            if ref_start_page is None and _REF_HEAD.match(text) and pageno > n * 0.5:
                ref_start_page = pageno
            elif ref_start_page is not None:
                ref_text_parts.append(text)

        # 表格
        try:
            for t in (page.find_tables() or []):
                cells = t.extract()
                md = _table_to_md(cells)
                if md:
                    tables.append({"id": f"t{len(tables)+1}", "page": pageno,
                                   "bbox": [round(v / pw, 4) if i % 2 == 0 else round(v / ph, 4)
                                            for i, v in enumerate(t.bbox)],
                                   "markdown": md})
        except Exception:
            pass

        # 图片 + caption 关联（图块下方最近文本块）
        for blk in raw.get("blocks", []):
            if blk["type"] != 1:
                continue
            x0, y0, x1, y1 = blk["bbox"]
            if (x1 - x0) * (y1 - y0) < pw * ph * 0.005:
                continue
            cap = ""
            for b in pblocks:
                if b["bbox"][1] >= y1 / ph and b["bbox"][1] - y1 / ph < 0.06:
                    cap = b["text"][:200]
                    break
            figures.append({"id": f"f{len(figures)+1}", "page": pageno, "caption": cap,
                            "bbox": [round(x0 / pw, 4), round(y0 / ph, 4),
                                     round(x1 / pw, 4), round(y1 / ph, 4)]})
        report(0.05 + 0.75 * (pageno + 1) / max(n, 1), f"解析第 {pageno+1}/{n} 页")

    toc = _build_toc(doc, blocks_by_page)

    # section_path 回填：按 toc（页面+标题序）为每个 block 推断所属章节
    sections = sorted(toc, key=lambda t: (t["page"], -t["level"]))
    si = -1
    for blk in blocks:
        while si + 1 < len(sections) and (
            sections[si + 1]["page"] < blk["page"]
            or (sections[si + 1]["page"] == blk["page"]
                and _page_y_of_title(blocks_by_page, sections[si + 1]["title"], blk["page"]) <= blk["bbox"][1])
        ):
            si += 1
        if si >= 0:
            blk["type"] = "heading" if blk["text"] == sections[si]["title"] else blk["type"]
            blk["section_path"] = sections[si]["title"]

    meta = _extract_meta(doc, blocks, blocks_by_page.get(0, []))
    docir: dict[str, Any] = {
        "paper_id": meta.get("arxiv_id") or pdf_path.stem,
        "meta": {**meta, "page_count": n, "file": pdf_path.name},
        "toc": toc,
        "blocks": blocks,
        "tables": tables,
        "figures": figures,
        "references": _parse_references("\n".join(ref_text_parts)) if ref_text_parts else [],
    }
    report(0.85, f"解析完成：{len(blocks)} blocks / {len(tables)} tables / {len(figures)} figures")
    return docir


def _page_y_of_title(blocks_by_page: dict[int, list[dict]], title: str, page: int) -> float:
    for b in blocks_by_page.get(page, []):
        if b["text"].strip() == title.strip():
            return b["bbox"][1]
    return 0.0


def _table_to_md(rows: list[list[str | None]]) -> str:
    rows = [[("" if c is None else str(c).replace("\n", " ").strip()) for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    md = "| " + " | ".join(rows[0]) + " |\n"
    md += "|" + "---|" * len(rows[0]) + "\n"
    for r in rows[1:]:
        md += "| " + " | ".join(r + [""] * (len(rows[0]) - len(r))) + " |\n"
    return md


_ARXIV_BANNER = re.compile(r"arxiv:\d{4}\.\d{4,5}", re.I)


def _extract_meta(doc: fitz.Document, blocks: list[dict], first_page_internal: list[dict]) -> dict:
    # arXiv id 可能在任意块（含被排到尾部的侧边横幅），全首页搜
    text = " ".join(b["text"] for b in blocks[:10]) + " " + \
           " ".join(b["text"] for b in first_page_internal)
    m = _ARXIV_ID.search(text)
    # 标题 = 首页上部（y<0.45）字号最大的非横幅块
    cands = [b for b in first_page_internal
             if b["bbox"][1] < 0.45 and not _ARXIV_BANNER.search(b["text"])
             and (b["bbox"][2] - b["bbox"][0]) > 0.15]
    title = ""
    if cands:
        best = max(cands, key=lambda b: b["size"])
        t = re.sub(r"\s+", " ", best["text"]).strip()
        if 8 <= len(t) <= 200 and not t[0].islower() and t.count("@") < 2:
            title = t
    return {"title": title or (doc.metadata.get("title") or ""),
            "authors": [], "arxiv_id": m.group(1) if m else "", "pub_date": "", "venue": ""}
