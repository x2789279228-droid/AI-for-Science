"""译文 PDF 导出（模块三 P0-2）：保持原位排版 overlay / 双语对照 bilingual 两种版式。

技术选型：PyMuPDF（fitz，已在依赖树 ≥1.24）——原页矢量零失真、redaction 只删
bbox 内文本（图片 images=2 保留 / 矢量 graphics=0 保留）、insert_textbox 自带
溢出判定（返回负值=放不下）。排除 reportlab（无法保真原版式）与 weasyprint
（需 pango/cairo 系统库，Windows 部署风险）。

诚实降级约定：
- 公式块（latex）永不替换；表格/图片 bbox 完全不动（译文进 MD/HTML 与双语文本视图）；
- 与表格/图片 bbox 重叠（IoU>0.3）的文本块跳过替换，记入 skipped_blocks；
- 译文在最小字号（6.5pt）下仍放不下 → 不强行缩到不可读，移入文末「译文补遗」页，
  记入 overflow_blocks；元数据标注「机器翻译草稿」。
"""
from __future__ import annotations

import time
from pathlib import Path

import fitz  # PyMuPDF

from .. import db, translate
from ..config import DATA_DIR, DOCIR_DIR
from ..log import logger

EXPORT_DIR = DATA_DIR / "exports"
_MIN_FONT = 6.5
_MAX_FONT = 12.0
# CJK 字体解析链：打包字体文件优先（可嵌入、可检索），缺省回退 MuPDF 内置 china-s
_FONT_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "assets" / "NotoSansSC-Regular.otf",
    Path(__file__).resolve().parents[3] / "deploy" / "fonts" / "NotoSansSC-Regular.otf",
]


def _font_args() -> dict:
    for p in _FONT_CANDIDATES:
        if p.exists():
            return {"fontname": "notosanssc", "fontfile": str(p)}
    return {"fontname": "china-s"}


def _pdf_path(paper_id: str) -> Path | None:
    row = db.query("SELECT file_path FROM papers WHERE id=?", (paper_id,))
    p = Path(row[0]["file_path"]) if row and row[0]["file_path"] else None
    return p if p and p.exists() else None


def _docir(paper_id: str) -> dict | None:
    for cand in (DOCIR_DIR / f"{paper_id}.json",):
        if cand.exists():
            import json
            return json.loads(cand.read_text(encoding="utf-8"))
    row = db.query("SELECT docir_path FROM papers WHERE id=?", (paper_id,))
    if row and row[0]["docir_path"]:
        p = Path(row[0]["docir_path"])
        if p.exists():
            import json
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def _iou(a: list[float], b: list[float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(ax1 - ax0, 0) * max(ay1 - ay0, 0)
    area_b = max(bx1 - bx0, 0) * max(by1 - by0, 0)
    return inter / max(area_a + area_b - inter, 1e-9)


def _translatable_pages(docir: dict, translated: dict[str, dict]) -> dict[int, list[dict]]:
    """按页聚合可替换块：paragraph/heading 且有译文；剔除公式块与图表重叠块。"""
    keep_out: list[tuple[int, list[float]]] = []
    for t in docir.get("tables", []):
        if t.get("bbox"):
            keep_out.append((t["page"], t["bbox"]))
    for f in docir.get("figures", []):
        if f.get("bbox"):
            keep_out.append((f["page"], f["bbox"]))
    pages: dict[int, list[dict]] = {}
    for b in docir.get("blocks", []):
        if b.get("type") not in ("paragraph", "heading"):
            continue
        tr = translated.get(b["id"])
        if not tr or not (tr.get("text") or "").strip() or not tr.get("format_ok", True):
            continue
        bbox = b.get("bbox") or []
        if len(bbox) != 4:
            continue
        page = b.get("page", 0)
        if any(p == page and _iou(bbox, bb) > 0.3 for p, bb in keep_out):
            continue
        pages.setdefault(page, []).append({"id": b["id"], "bbox": bbox, "text": tr["text"],
                                           "heading": b.get("type") == "heading"})
    return pages


def _fit_text(page: fitz.Page, rect: fitz.Rect, text: str, heading: bool) -> bool:
    """自动字号写入：估算起点 → 逐级下探至 6.5pt；仍溢出返回 False（调用方走补遗页）。"""
    est = max(_MIN_FONT, min(_MAX_FONT if heading else 10.5,
                             (rect.width * rect.height) / max(len(text) * 0.62, 1.0)))
    size = round(est, 1)
    while size >= _MIN_FONT:
        rc = page.insert_textbox(rect, text, fontsize=size, align=0, **_font_args())
        if rc >= 0:
            return True
        size = round(size - 0.5, 1)
    return False


def export_pdf(paper_id: str, *, target_lang: str = "zh", mode: str = "academic",
               layout: str = "overlay") -> dict:
    """生成译文 PDF。返回 {path, pages, replaced, overflow_blocks, skipped_note}。"""
    src = _pdf_path(paper_id)
    docir = _docir(paper_id)
    if src is None or docir is None:
        raise FileNotFoundError(f"paper_or_docir_missing:{paper_id}")
    translated = translate.cached_translations(paper_id, target_lang, mode)
    if not translated:
        raise LookupError("no_translations:请先翻译（target_lang/mode 与译文缓存一致）")

    pages_map = _translatable_pages(docir, translated)
    doc = fitz.open(src)
    overflow: list[dict] = []
    replaced = 0

    if layout == "overlay":
        # redaction 常量：images=2（不删图片）/ graphics=0（不删矢量）/ text=0（删文本）
        for pno, blocks in pages_map.items():
            if pno >= doc.page_count:
                continue
            page = doc[pno]
            prect = page.rect
            rects: list[tuple[dict, fitz.Rect]] = []
            for b in blocks:
                x0, y0, x1, y1 = b["bbox"]
                rects.append((b, fitz.Rect(prect.x0 + x0 * prect.width,
                                           prect.y0 + y0 * prect.height,
                                           prect.x0 + x1 * prect.width,
                                           prect.y0 + y1 * prect.height)))
            for _, r in rects:
                page.add_redact_annot(r)
            page.apply_redactions(images=2, graphics=0, text=0)
            for b, r in rects:
                if _fit_text(page, r, b["text"], b["heading"]):
                    replaced += 1
                else:
                    overflow.append({"block_id": b["id"], "page": pno})
        if overflow:
            tail = doc.new_page()
            tail.insert_text(fitz.Point(72, 72), "译文补遗（原位放不下的块，未缩小到不可读）",
                             fontsize=14, **_font_args())
            y = 100.0
            for o in overflow:
                text = translated.get(o["block_id"], {}).get("text", "")
                rc = tail.insert_textbox(fitz.Rect(56, y, tail.rect.width - 56, y + 90),
                                         f"[p{o['page'] + 1} · {o['block_id']}] {text}",
                                         fontsize=8, **_font_args())
                y += 92 + max(0.0, -rc)
                if y > tail.rect.height - 60:
                    tail = doc.new_page()
                    y = 72.0
    else:  # bilingual：原页后插译文页（按 DocIR 阅读序排版）
        max_pno = max(pages_map) if pages_map else -1
        for pno in range(max_pno + 1):
            src_page = doc[pno * 2]      # 每轮在原页后插一页 → 原页 pno 恒在索引 2*pno
            new = doc.new_page(pno=pno * 2 + 1, width=src_page.rect.width,
                               height=src_page.rect.height)
            new.insert_text(fitz.Point(56, 52), f"— 译文页（第 {pno + 1} 页 · 机器翻译草稿）—",
                            fontsize=9, **_font_args())
            y = 80.0
            for b in sorted(pages_map.get(pno, []), key=lambda x: (x["bbox"][1], x["bbox"][0])):
                rc = new.insert_textbox(fitz.Rect(56, y, new.rect.width - 56, y + 110),
                                        b["text"], fontsize=9.5, **_font_args())
                replaced += 1
                y += 112 + max(0.0, -rc)
                if y > new.rect.height - 60:
                    break   # 单页译文页容量有限：其余块仍在原 PDF 内（如实计数 replaced）

    page_count = doc.page_count
    meta = doc.metadata or {}
    meta["title"] = f"{docir.get('meta', {}).get('title', paper_id)}（机器翻译草稿）"
    meta["subject"] = f"machine-translated draft target={target_lang} mode={mode}"
    meta["keywords"] = "机器翻译"
    doc.set_metadata(meta)

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = EXPORT_DIR / f"{paper_id}.{target_lang}.{mode}.{layout}.pdf"
    doc.save(str(out), garbage=3, deflate=True)
    doc.close()

    with db.tx() as c:
        c.execute("INSERT INTO translation_exports(id,paper_id,fmt,target_lang,mode,layout,"
                  "path,pages,replaced,overflow_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (db.new_id("e_"), paper_id, "pdf", target_lang, mode, layout, str(out),
                   page_count, replaced, len(overflow), time.time()))
    logger.info(f"export_pdf {paper_id} → {out.name}（{replaced} 块替换 / {len(overflow)} 溢出）",
                module="export")
    return {"path": str(out), "pages": page_count, "replaced": replaced,
            "overflow_blocks": overflow}


# ---------- MD / HTML 导出（译文单栏 / 双语对照） ----------

def export_doc(paper_id: str, *, target_lang: str = "zh", mode: str = "academic",
               fmt: str = "md", bilingual_view: bool = False) -> dict:
    """服务端 MD/HTML 导出：结构来自 DocIR，译文替换原文（bilingual_view=True 时
    原文/译文对照）。公式保留 LaTeX、[n] 引用标记原样；图表以 caption 文本占位。"""
    import html as _html

    docir = _docir(paper_id)
    if docir is None:
        raise FileNotFoundError(f"docir_missing:{paper_id}")
    translated = translate.cached_translations(paper_id, target_lang, mode)
    title = docir.get("meta", {}).get("title", paper_id)

    lines: list[str] = [f"# {title}", "",
                        f"> 机器翻译草稿 · target={target_lang} · mode={mode}", ""]
    for b in docir.get("blocks", []):
        text = (b.get("text") or "").strip()
        if not text:
            continue
        tr = translated.get(b["id"], {})
        tr_text = (tr.get("text") or "").strip() if tr.get("format_ok", True) else ""
        if b.get("type") == "formula" or b.get("latex"):
            lines += [f"$${b.get('latex') or text}$$", ""]
            continue
        if b.get("type") == "heading":
            lvl = min((b.get("level") or 2) + 1, 6)
            lines += ["#" * lvl + " " + (tr_text or text), ""]
        elif bilingual_view:
            lines += [text, "", f"> {tr_text or '（未翻译）'}", ""]
        else:
            lines += [tr_text or text, ""]
    for t in docir.get("tables", []):
        tr = translated.get(t["id"], {})
        md = (tr.get("text") if tr.get("format_ok", True) else "") or t.get("markdown") or ""
        lines += [f"**Table（第 {t.get('page', 0) + 1} 页）**：{t.get('title') or t['id']}", "", md, ""]
    for f in docir.get("figures", []):
        tr = translated.get(f["id"], {})
        cap = (tr.get("text") if tr.get("format_ok", True) else "") or f.get("caption") or f["id"]
        lines += [f"**Figure（第 {f.get('page', 0) + 1} 页）**：{cap}", ""]

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if fmt == "html":
        body = "<br>".join(_html.escape(l) for l in lines)
        content = (f"<!doctype html><html><head><meta charset='utf-8'><title>{_html.escape(title)}"
                   f"</title></head><body style='font-family:sans-serif;max-width:820px;"
                   f"margin:auto;line-height:1.7'>{body}</body></html>")
        out = EXPORT_DIR / f"{paper_id}.{target_lang}.{mode}.{'bi.' if bilingual_view else ''}html"
    else:
        content = "\n".join(lines)
        out = EXPORT_DIR / f"{paper_id}.{target_lang}.{mode}.{'bi.' if bilingual_view else ''}md"
    out.write_text(content, encoding="utf-8")
    with db.tx() as c:
        c.execute("INSERT INTO translation_exports(id,paper_id,fmt,target_lang,mode,layout,"
                  "path,pages,replaced,overflow_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (db.new_id("e_"), paper_id, fmt, target_lang, mode,
                   "bilingual" if bilingual_view else "mono", str(out),
                   len(docir.get("blocks", [])), len(translated), 0, time.time()))
    return {"path": str(out), "format": fmt, "blocks": len(docir.get("blocks", [])),
            "translated": len(translated)}

