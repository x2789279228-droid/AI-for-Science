"""解析路由（方案 docs/01 §4.1 解析 Router）。

输入 PDF → probe（文本层/字符密度/图片占比/页数）→ 路由：
  页数 >200                 → 长文档模式（longdoc.py：50 页/批流式，批内仍按文本层选引擎）
  平均字符 >300 且图片占比 <0.6 → 原生通道（MinerU 开启则 MinerU，否则 PyMuPDF 快速路径）
  平均字符 <100 或图片占比 >0.6 → 扫描件通道（MinerU OCR / Qwen-VL 逐页 / 诚实标注兜底）
  100~300 灰区              → PyMuPDF 主路 + 无文本层页逐页 VLM 救援
  任何引擎异常              → PyMuPDF 兜底（保证不空）

parse_document() 是 jobs._run_parse 的唯一入口；DocIR schema 与既有下游完全兼容，
meta 仅新增字段（parse_engine/parse_quality/scanned/long_doc/quality）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from . import longdoc, mineru_engine, pipeline, quality, vlm_engine
from ._types import CancelCheck, ParseCancelled, ProgressCb

__all__ = ["ParseCancelled", "ProgressCb", "parse_document", "probe_pdf", "route",
           "write_docir"]

LONG_DOC_PAGES = 200          # 超过 → 长文档模式（docs/01 §4.1）
NATIVE_MIN_CHARS = 300        # 平均字符 ≥ → 原生通道
SCANNED_MAX_CHARS = 100       # 平均字符 ≤ 或图片占比 > → 扫描件通道
SCANNED_IMAGE_RATIO = 0.6


def probe_pdf(pdf_path: str | Path) -> dict:
    """采样探测：平均每页字符数 / 图片面积占比 / 页数 / 是否有文本层。"""
    doc = fitz.open(pdf_path)
    try:
        n = doc.page_count
        sample_pages = min(10, n)
        chars = 0
        img_area = 0.0
        page_area = 0.0
        blank_pages: list[int] = []
        for i in range(sample_pages):
            page = doc[i]
            t = page.get_text("text").strip()
            chars += len(t)
            if len(t) < 50:
                blank_pages.append(i)
            pw, ph = page.rect.width, page.rect.height
            page_area += pw * ph
            for blk in page.get_text("dict").get("blocks", []):
                if blk.get("type") == 1:  # image
                    x0, y0, x1, y1 = blk["bbox"]
                    img_area += max(0.0, (x1 - x0)) * max(0.0, (y1 - y0))
        return {
            "page_count": n,
            "avg_chars": round(chars / max(sample_pages, 1), 1),
            "image_ratio": round(img_area / max(page_area, 1.0), 3),
            "has_text_layer": pipeline._has_text_layer(doc),
            "sample_blank_pages": blank_pages,
        }
    finally:
        doc.close()


def route(probe: dict) -> dict:
    """探测结果 → 路由决策 {mode, engine, long}。mode: native|scanned|gray。

    engine 为预期主引擎（与各 _parse_* 分支的实际选择一致）：
    native/long → MinerU(可用)否则 PyMuPDF；scanned → MinerU OCR(可用)否则
    VLM(可用)否则 PyMuPDF 诚实兜底。
    """
    long = probe["page_count"] > LONG_DOC_PAGES
    if probe["avg_chars"] >= NATIVE_MIN_CHARS and probe["image_ratio"] < SCANNED_IMAGE_RATIO:
        mode = "native"
    elif probe["avg_chars"] <= SCANNED_MAX_CHARS or probe["image_ratio"] > SCANNED_IMAGE_RATIO:
        mode = "scanned"
    else:
        mode = "gray"
    if mineru_engine.available():
        engine = "mineru"
    elif mode == "scanned" and vlm_engine.available():
        engine = "qwen-vl"
    else:
        engine = "pymupdf"
    return {"mode": mode, "engine": engine, "long": long}


def parse_document(pdf_path: str | Path, progress: ProgressCb | None = None,
                   paper_id: str = "", cancel_check: CancelCheck | None = None) -> dict:
    """解析总入口：probe → 路由 → 引擎执行 → 失败兜底 → meta/quality 填充。

    paper_id 用于长文档"大纲先出"的逐批落盘（reader 在解析中途即可见标题树）。
    cancel_check：协作式取消探测回调，引擎在阶段边界轮询；命中抛 ParseCancelled
    （不触发 PyMuPDF 兜底——取消不是失败）。
    """
    pdf_path = Path(pdf_path)
    probe = probe_pdf(pdf_path)
    decision = route(probe)
    report: ProgressCb = progress or (lambda f, m: None)
    report(0.03, f"路由：{decision['mode']} · 引擎 {decision['engine']}"
                 + (" · 长文档模式" if decision["long"] else "")
                 + f" · {probe['page_count']} 页 / 均 {probe['avg_chars']} 字符")

    docir: dict[str, Any] | None = None
    try:
        if decision["long"]:
            docir = longdoc.parse_long_pdf(pdf_path, probe, decision, progress,
                                           paper_id=paper_id, cancel_check=cancel_check)
        elif decision["mode"] == "scanned":
            docir = _parse_scanned(pdf_path, probe, progress, cancel_check)
        else:
            docir = _parse_native_or_gray(pdf_path, probe, decision, progress, cancel_check)
    except ParseCancelled:
        raise
    except Exception as e:  # noqa: BLE001 —— 解析失败兜底：PyMuPDF 纯文本（保证不空）
        report(0.5, f"{decision['engine']} 引擎失败（{type(e).__name__}）→ PyMuPDF 兜底")
        docir = pipeline.parse_pdf(pdf_path, progress=progress, cancel_check=cancel_check)
        docir["meta"]["parse_engine"] = "pymupdf"
        docir["meta"]["fallback_reason"] = f"{decision['engine']}:{type(e).__name__}"

    meta = docir.setdefault("meta", {})
    meta.update({
        "page_count": meta.get("page_count") or probe["page_count"],
        "route_mode": decision["mode"],
        "long_doc": decision["long"],
        "scanned": decision["mode"] == "scanned",
    })
    if decision["mode"] == "gray":
        meta.setdefault("scanned_pages", [])
        if meta["scanned_pages"]:
            meta["scanned"] = True
    q = quality.compute(docir, probe)
    meta["quality"] = q
    meta["parse_quality"] = q.pop("overall", 0.0)
    report(0.85, f"解析完成（{meta.get('parse_engine')}）：{len(docir['blocks'])} blocks / "
                 f"{len(docir['tables'])} tables / {len(docir['figures'])} figures")
    return docir


def _parse_native_or_gray(pdf_path: Path, probe: dict, decision: dict,
                          progress: ProgressCb, cancel_check: CancelCheck | None) -> dict:
    if decision["engine"] == "mineru":
        docir = mineru_engine.parse(pdf_path, progress=progress, cancel_check=cancel_check)
        docir["meta"]["parse_engine"] = "mineru"
        return docir
    docir = pipeline.parse_pdf(pdf_path, progress=progress, cancel_check=cancel_check)
    docir["meta"]["parse_engine"] = "pymupdf"
    if decision["mode"] == "gray" and vlm_engine.available():
        # 灰区逐页救援：无文本层的页 → Qwen-VL（只对失败页调用，评分表口径）
        docir = vlm_engine.rescue_blank_pages(pdf_path, docir, progress,
                                              cancel_check=cancel_check)
        docir["meta"]["parse_engine"] = "mixed" if docir["meta"].get("rescued_pages") else "pymupdf"
    return docir


def _parse_scanned(pdf_path: Path, probe: dict, progress: ProgressCb,
                   cancel_check: CancelCheck | None) -> dict:
    if mineru_engine.available():
        docir = mineru_engine.parse(pdf_path, progress=progress, cancel_check=cancel_check)
        docir["meta"]["parse_engine"] = "mineru"
        docir["meta"]["scanned_pages"] = list(range(probe["page_count"]))
        return docir
    if vlm_engine.available():
        docir = vlm_engine.parse_scanned(pdf_path, progress=progress, cancel_check=cancel_check)
        docir["meta"]["parse_engine"] = "qwen-vl"
        docir["meta"]["scanned_pages"] = docir["meta"].get("scanned_pages") or \
            list(range(probe["page_count"]))
        return docir
    # 无 MinerU 无 VLM key：PyMuPDF 空层 + 诚实标注（保证不空、不冒充）
    docir = pipeline.parse_pdf(pdf_path, progress=progress)
    docir["meta"]["parse_engine"] = "pymupdf"
    docir["meta"]["scanned_pages"] = list(range(probe["page_count"]))
    docir["meta"]["scanned_note"] = "扫描件且未配置 OCR/VLM：文本层缺失，内容可能不完整"
    return docir


def write_docir(paper_id: str, docir: dict) -> Path:
    """DocIR 落盘（长文档逐批重写也走这里）。"""
    from ..config import DOCIR_DIR

    DOCIR_DIR.mkdir(parents=True, exist_ok=True)
    path = DOCIR_DIR / f"{paper_id}.json"
    path.write_text(json.dumps(docir, ensure_ascii=False), encoding="utf-8")
    return path
