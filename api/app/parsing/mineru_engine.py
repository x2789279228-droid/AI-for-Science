"""MinerU 原生解析引擎（方案 docs/01 §4.2：middle.json 直接映射 DocIR）。

- 子进程调隔离 venv 的 mineru 2.x CLI（-p pdf -o out -m auto），权重经 modelscope
- middle.json → DocIR：title/text/equation/table/image 块映射，bbox 由 page_size 归一化
- bbox 标定自检：抽文本层页与 PyMuPDF 文本块算 IoU，<0.5 自动尝试 y 翻转（ADR-001）
- 公式：interline_equation → type=formula（latex 进 text，KaTeX/RAG 两用）
- 表格：html → markdown + headers/rows（viz 直接可用）；图片块 → figures（img_path 留痕）
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from ..config import DOCIR_DIR, settings
from ._types import ProgressCb
from .pipeline import _REF_HEAD, _parse_references, backfill_sections
from .tables import html_table_to_rows, table_to_md


def available() -> bool:
    """MINERU_ENABLED=on 强制启用；off 关闭；auto=检测到 CLI 才启用。"""
    if settings.mineru_enabled == "off":
        return False
    if settings.mineru_enabled == "on":
        return True
    return Path(settings.mineru_cmd).exists()


def parse(pdf_path: str | Path, progress: ProgressCb | None = None,
          page_offset: int = 0, out_dir: str | Path | None = None,
          timeout_s: int | None = None) -> dict:
    """MinerU CLI 解析 → DocIR。page_offset：子 PDF 批次解析时块页码换算。

    timeout_s：覆盖默认超时（长文档整本调用按页数扩展，见 longdoc._parse_whole_mineru）。
    """
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir) if out_dir else DOCIR_DIR.parent / "parsing" / \
        f"{pdf_path.stem}_{int(time.time())}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if progress:
        progress(0.05, f"MinerU 解析中（{pdf_path.name}）…")
    env = dict(os.environ)
    env["MINERU_MODEL_SOURCE"] = env.get("MINERU_MODEL_SOURCE", "modelscope")
    try:
        subprocess.run(
            [settings.mineru_cmd, "-p", str(pdf_path), "-o", str(out_dir), "-m", "auto"],
            check=True, capture_output=True, timeout=timeout_s or settings.mineru_timeout_s,
            env=env)
    except FileNotFoundError as e:
        raise RuntimeError(f"mineru CLI 不存在：{settings.mineru_cmd}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"MinerU 超时（>{timeout_s or settings.mineru_timeout_s}s）") from e

    middle_path = _find_middle(out_dir)
    if middle_path is None:
        raise RuntimeError(f"MinerU 未产出 middle.json（输出目录 {out_dir}）")
    if progress:
        progress(0.6, "MinerU 完成 → middle.json 映射 DocIR")
    middle = json.loads(middle_path.read_text(encoding="utf-8"))
    docir = middle_to_docir(middle, pdf_path, page_offset=page_offset,
                            base_dir=middle_path.parent)
    docir["meta"]["mineru_output"] = str(middle_path.parent)
    if progress:
        progress(0.75, f"middle.json → {len(docir['blocks'])} blocks / "
                       f"{len(docir['tables'])} tables / {len(docir['figures'])} figures")
    return docir


def _find_middle(out_dir: Path) -> Path | None:
    """MinerU 输出结构 {out}/{stem}/auto/{stem}_middle.json → rglob 兜底。"""
    hits = sorted(out_dir.rglob("*_middle.json"))
    return hits[0] if hits else None


# ---------- middle.json → DocIR 映射（纯函数，可单测） ----------

def middle_to_docir(middle: dict, pdf_path: str | Path, *,
                    page_offset: int = 0, use_pymupdf_toc: bool = True,
                    base_dir: str | Path | None = None) -> dict:
    """middle.json → DocIR。base_dir：MinerU 输出 auto 目录（image_path 相对它转绝对）。"""
    pdf_path = Path(pdf_path)
    pages = middle.get("pdf_info") or []
    base = Path(base_dir) if base_dir else None

    transform = "none"
    try:
        transform = _calibrate(pages, pdf_path, page_offset)
    except Exception:  # noqa: BLE001 —— 标定失败按无变换处理
        transform = "none"

    blocks: list[dict] = []
    tables: list[dict] = []
    figures: list[dict] = []
    pages_with_text: set[int] = set()
    for page in pages:
        page_idx = int(page.get("page_idx", 0)) + page_offset
        pw, ph = page.get("page_size") or (1.0, 1.0)
        pw, ph = float(pw) or 1.0, float(ph) or 1.0
        for blk in page.get("preproc_blocks") or []:
            btype = blk.get("type", "text")
            bbox = _norm_bbox(blk.get("bbox") or [0, 0, 0, 0], pw, ph, transform)
            if btype == "title":
                text = _block_text(blk)
                if not text:
                    continue
                blocks.append({"type": "heading", "text": text, "latex": None,
                               "page": page_idx, "bbox": bbox, "section_path": ""})
                pages_with_text.add(page_idx)
            elif btype in ("equation", "interline_equation"):
                latex = _block_latex(blk)
                if not latex:
                    continue
                latex = latex.strip().strip("$").strip()
                blocks.append({"type": "formula", "text": latex, "latex": latex,
                               "page": page_idx, "bbox": bbox, "section_path": ""})
                pages_with_text.add(page_idx)
            elif btype == "table":
                entry = _table_entry(blk, page_idx, bbox, len(tables))
                if entry:
                    tables.append(entry)
                    pages_with_text.add(page_idx)
            elif btype == "image":
                entry = _figure_entry(blk, page_idx, bbox, len(figures), base)
                if entry:
                    figures.append(entry)
            else:  # text 及其他 → 正文段落
                text = _block_text(blk)
                if not text:
                    continue
                blocks.append({"type": "paragraph", "text": text, "latex": None,
                               "page": page_idx, "bbox": bbox, "section_path": ""})
                pages_with_text.add(page_idx)

    for i, b in enumerate(blocks, 1):
        b["id"] = f"b{i}"
    for i, t in enumerate(tables, 1):
        t["id"] = f"t{i}"
    for i, f in enumerate(figures, 1):
        f["id"] = f"f{i}"

    # toc/章节回填：文本层存在时用 PyMuPDF 字号层级（层级准）；
    # 扫描件（无文本层）退化为标题块平铺 level=1
    toc: list[dict] = []
    arxiv_id = ""
    doc = fitz.open(pdf_path)
    try:
        has_text = sum(len(p.get_text("text").strip()) for p in doc) > 50 * doc.page_count
        if has_text:
            # arXiv id 从 PDF 首页文本层直接取（MinerU 把侧边横幅归入 discarded_blocks）
            from .pipeline import _ARXIV_ID
            m_arxiv = _ARXIV_ID.search(doc[0].get_text("text"))
            arxiv_id = (m_arxiv.group(1) + (m_arxiv.group(2) or "")) if m_arxiv else ""
            if use_pymupdf_toc:
                if page_offset:
                    # 子 PDF 批次：书签页码为相对值，手动 +offset；无书签回退字号层级
                    toc = []
                    try:
                        for lvl, title_b, page_b in doc.get_toc():
                            toc.append({"level": min(int(lvl), 3), "title": title_b.strip(),
                                        "page": max(int(page_b), 1) - 1 + page_offset})
                    except Exception:  # noqa: BLE001 —— 书签损坏则换字号树重建
                        toc = []
                if not toc:
                    pm_blocks: dict[int, list[dict]] = {}
                    for pageno in range(doc.page_count):
                        page = doc[pageno]
                        pw, ph = page.rect.width, page.rect.height
                        pb = []
                        for blk in page.get_text("dict").get("blocks", []):
                            if blk.get("type") != 0:
                                continue
                            lines = blk.get("lines", [])
                            text = "\n".join("".join(s["text"] for s in ln["spans"])
                                             for ln in lines).strip()
                            if not text:
                                continue
                            pb.append({"bbox": [blk["bbox"][0] / pw, blk["bbox"][1] / ph,
                                                blk["bbox"][2] / pw, blk["bbox"][3] / ph],
                                       "lines": lines, "text": text, "size": 10.0})
                        # 子 PDF 批次解析：toc 页码与块一致换算为文档绝对页码
                        pm_blocks[pageno + page_offset] = pb
                    toc = _build_toc_safe(doc, pm_blocks)
    finally:
        doc.close()
    if not toc:
        seen: set[str] = set()
        for b in blocks:
            if b["type"] == "heading" and b["text"] not in seen:
                seen.add(b["text"])
                toc.append({"level": 1, "title": b["text"], "page": b["page"]})

    blocks_by_page: dict[int, list[dict]] = {}
    for b in blocks:
        blocks_by_page.setdefault(b["page"], []).append(b)
    backfill_sections(blocks, toc, blocks_by_page)

    # References：References 类标题块之后的正文
    ref_parts: list[str] = []
    in_ref = False
    for b in blocks:
        if b["type"] == "heading" and _REF_HEAD.match(b["text"].strip()):
            in_ref = True
            continue
        if in_ref and b["type"] == "paragraph":
            ref_parts.append(b["text"])

    title = next((b["text"] for b in blocks if b["type"] == "heading"), "") or pdf_path.stem
    docir: dict[str, Any] = {
        "paper_id": pdf_path.stem,
        "meta": {"title": title, "authors": [], "arxiv_id": arxiv_id,
                 "pub_date": "", "venue": "", "page_count": len(pages), "file": pdf_path.name,
                 "bbox_transform": transform},
        "toc": toc,
        "blocks": blocks,
        "tables": tables,
        "figures": figures,
        "references": _parse_references("\n".join(ref_parts)) if ref_parts else [],
    }
    return docir


def _build_toc_safe(doc: fitz.Document, blocks_by_page: dict[int, list[dict]]) -> list[dict]:
    from .pipeline import _build_toc
    return _build_toc(doc, blocks_by_page)


def _norm_bbox(bbox: list[float], pw: float, ph: float, transform: str) -> list[float]:
    x0, y0, x1, y1 = bbox
    if transform == "flip":
        y0, y1 = ph - y1, ph - y0
    return [round(max(0.0, min(1.0, x0 / pw)), 4), round(max(0.0, min(1.0, y0 / ph)), 4),
            round(max(0.0, min(1.0, x1 / pw)), 4), round(max(0.0, min(1.0, y1 / ph)), 4)]


def _block_text(blk: dict) -> str:
    """块内文本：行按 \n 连接，行内公式 span 用 $latex$ 内嵌。"""
    lines: list[str] = []
    for ln in blk.get("lines") or []:
        parts: list[str] = []
        for sp in ln.get("spans") or []:
            if sp.get("type") == "inline_equation" and sp.get("latex"):
                parts.append(f"${sp['latex']}$")
            else:
                parts.append(str(sp.get("content") or ""))
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _block_latex(blk: dict) -> str:
    for ln in blk.get("lines") or []:
        for sp in ln.get("spans") or []:
            if sp.get("latex"):
                return str(sp["latex"])
            if sp.get("type") == "interline_equation" and sp.get("content"):
                return str(sp["content"])
    return ""


def _subblock_texts(blk: dict, kind: str) -> list[str]:
    """MinerU 表格/图片块的 caption 子块文本。"""
    out: list[str] = []
    for sub in blk.get("blocks") or []:
        if sub.get("type") == kind:
            t = _block_text(sub)
            if t:
                out.append(t)
    return out


def _table_entry(blk: dict, page_idx: int, bbox: list[float], seq: int) -> dict | None:
    """表格块 → tables[]：html → markdown + headers/rows。"""
    html = ""
    for ln in blk.get("lines") or []:
        for sp in ln.get("spans") or []:
            if sp.get("html"):
                html = sp["html"]
                break
        if html:
            break
    if not html:
        for sub in blk.get("blocks") or []:
            for ln in sub.get("lines") or []:
                for sp in ln.get("spans") or []:
                    if sp.get("html"):
                        html = sp["html"]
                        break
                if html:
                    break
            if html:
                break
    if not html:
        return None
    headers, rows = html_table_to_rows(html)
    md = table_to_md([headers] + rows) if headers else ""
    if not md:
        return None
    caption = "；".join(_subblock_texts(blk, "table_caption"))[:200]
    return {"page": page_idx, "bbox": bbox, "markdown": md, "html": html[:8000],
            **({"headers": headers, "rows": rows} if headers else {}),
            **({"title": caption} if caption else {})}


def _figure_entry(blk: dict, page_idx: int, bbox: list[float], seq: int,
                  base_dir: Path | None = None) -> dict | None:
    """图片块 → figures[]：caption 关联 + img_path（相对 auto 目录 → 绝对，供端点与 reader）。"""
    img_path = ""
    for sub in blk.get("blocks") or []:
        for ln in sub.get("lines") or []:
            for sp in ln.get("spans") or []:
                if sp.get("type") == "image" and sp.get("image_path"):
                    img_path = sp["image_path"]
                    break
    caption = "；".join(_subblock_texts(blk, "image_caption"))[:200]
    if not caption and not img_path:
        return None
    entry = {"page": page_idx, "bbox": bbox, "caption": caption}
    if img_path:
        p = Path(img_path)
        if base_dir and not p.is_absolute():
            # 2.5.4 实测：middle.json 里是裸文件名，裁图实际在 auto/images/ 下
            p = base_dir / p if (base_dir / p).exists() else base_dir / "images" / p
        entry["image_path"] = str(p)
    return entry


# ---------- bbox 标定（ADR-001：docs/05 D0 卡点2「地脚螺栓」） ----------

def _calibrate(pages: list[dict], pdf_path: Path, page_offset: int) -> str:
    """抽一个文本层页，比较 MinerU 文本块与 PyMuPDF 文本块的平均 IoU。

    返回应使用的坐标变换："none" 或 "flip"（y 轴翻转）。无文本层页 → "none"。
    """
    doc = fitz.open(pdf_path)
    try:
        for page in pages[:3]:
            page_idx = int(page.get("page_idx", 0))
            if page_idx >= doc.page_count:
                continue
            pym = doc[page_idx + page_offset]
            pw, ph = page.get("page_size") or (pym.rect.width, pym.rect.height)
            pw, ph = float(pw) or 1.0, float(ph) or 1.0
            pm_rects = []
            for blk in pym.get_text("dict").get("blocks", []):
                if blk.get("type") != 0 or not blk.get("lines"):
                    continue
                pm_rects.append(blk["bbox"])
            if not pm_rects:
                continue
            mu_rects = [b.get("bbox") for p in pages if int(p.get("page_idx", 0)) == page_idx
                        for b in (p.get("preproc_blocks") or [])
                        if b.get("type") in ("text", "title") and b.get("bbox")]
            if not mu_rects:
                continue
            iou_none = _mean_iou(mu_rects, pm_rects)
            flipped = [[b[0], ph - b[3], b[2], ph - b[1]] for b in mu_rects]
            iou_flip = _mean_iou(flipped, pm_rects)
            return "flip" if iou_flip > iou_none and iou_flip > 0.5 else "none"
        return "none"
    finally:
        doc.close()


def _mean_iou(rects_a: list[list[float]], rects_b: list[float]) -> float:
    """A 中每块取与 B 的最大 IoU 再平均（抽样 ≤40 块控制耗时）。"""
    ious = []
    for a in rects_a[:40]:
        best = 0.0
        for b in rects_b[:80]:
            best = max(best, _iou(a, b))
        ious.append(best)
    return sum(ious) / len(ious) if ious else 0.0


def _iou(a: list[float], b: list[float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / max(area_a + area_b - inter, 1e-9)
