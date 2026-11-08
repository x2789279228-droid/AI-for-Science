"""长文档流式分批解析（docs/01 §4.2 长文档通道 + docs/05 降级矩阵）。

- PyMuPDF 引擎：50 页/批页范围直解析，批级进度细粒度可见
- MinerU 引擎：整本单次 CLI 调用（模型只加载一次，CPU 上远快于逐批子进程），
  运行期间 15s 心跳上报进度；解析完一次性合并落盘
- 目录先出：书签 toc 骨架先落盘（秒级），reader 立即可见大纲，正文随后追加
- 索引降级（前 N 页全文 chunk）由 jobs 层按 meta.long_doc 处理
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import fitz  # PyMuPDF

from ..config import DOCIR_DIR, settings
from . import mineru_engine, pipeline, vlm_engine
from ._types import ProgressCb

BATCH_PAGES = 50   # docs/05：每 50 页一个批次（PyMuPDF 通道；进度/降级叙事单位）


def parse_long_pdf(pdf_path: Path, probe: dict, decision: dict,
                   progress: ProgressCb, paper_id: str = "") -> dict:
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)
    n = doc.page_count
    total_batches = (n + BATCH_PAGES - 1) // BATCH_PAGES

    # ---- 阶段 1：目录骨架先出（书签 toc 秒级可得；无书签则解析中渐进补齐） ----
    toc: list[dict] = []
    try:
        for lvl, title, page in doc.get_toc():
            toc.append({"level": min(int(lvl), 3), "title": title.strip(),
                        "page": max(int(page), 1) - 1})   # 0 基
    except Exception:  # noqa: BLE001, S110 —— 书签解析失败由 PyMuPDF 字号树兜底
        pass

    docir: dict = {
        "paper_id": pdf_path.stem,
        "meta": {"title": pdf_path.stem, "authors": [], "arxiv_id": "", "pub_date": "",
                 "venue": "", "page_count": n, "file": pdf_path.name,
                 "parse_engine": decision["engine"]},
        "toc": toc, "blocks": [], "tables": [], "figures": [], "references": [],
    }
    if toc and paper_id:
        _write(paper_id, docir)   # 大纲先行：上传后秒级可见标题树
    progress(0.06, f"大纲已出（{len(toc)} 条书签）· 开始解析 {n} 页"
                   f"（{decision['engine']} · 共 {total_batches} 批）")

    # ---- 阶段 2：正文解析（MinerU 整本单次 / PyMuPDF 逐批） ----
    if decision["engine"] == "mineru" and mineru_engine.available():
        _parse_whole_mineru(doc, pdf_path, decision, progress, paper_id, docir, toc)
    else:
        _parse_batched_pymupdf(doc, pdf_path, decision, progress, paper_id, docir, toc)

    # ---- 阶段 3：灰区/扫描救援（空白页 → VLM，只对失败页调用） ----
    if decision["mode"] == "gray" and vlm_engine.available():
        docir = vlm_engine.rescue_blank_pages(pdf_path, docir, progress)
        if docir["meta"].get("rescued_pages"):
            docir["meta"]["parse_engine"] = "mixed"

    progress(0.85, f"长文档解析完成：{len(docir['blocks'])} blocks / "
                   f"{len(docir['tables'])} tables / toc {len(docir['toc'])} 条")
    doc.close()
    return docir


def _merge_part(docir: dict, part: dict, blocks_by_page: dict[int, list[dict]] | None) -> None:
    """解析产物合并进主 DocIR（id 全局重排；refs 按 index 去重）。"""
    seen = {r["index"] for r in docir["references"]}
    for b in part["blocks"]:
        b["id"] = f"b{len(docir['blocks']) + 1}"
        docir["blocks"].append(b)
        if blocks_by_page is not None:
            blocks_by_page.setdefault(b["page"], []).append(b)
    for t in part["tables"]:
        t["id"] = f"t{len(docir['tables']) + 1}"
        docir["tables"].append(t)
    for f in part["figures"]:
        f["id"] = f"f{len(docir['figures']) + 1}"
        docir["figures"].append(f)
    for r in part.get("references", []):
        if r["index"] not in seen:
            seen.add(r["index"])
            docir["references"].append(r)


def _parse_whole_mineru(doc: fitz.Document, pdf_path: Path, decision: dict,
                        progress: ProgressCb, paper_id: str, docir: dict, toc: list[dict]) -> None:
    """MinerU 整本单次调用：模型只加载一次；心跳线程保进度可见。"""
    n = doc.page_count
    estimate_s = max(n, 60)   # CPU 粗估 ~1s/页（含 2-3 分钟模型加载）
    stop = threading.Event()
    t0 = time.time()

    def pulse() -> None:
        tick = 0
        while not stop.wait(15):
            tick += 1
            elapsed = time.time() - t0
            frac = min(0.08 + 0.72 * elapsed / estimate_s, 0.78)
            progress(frac, f"MinerU 解析整本中… 已运行 {int(elapsed)}s / 粗估 {estimate_s}s")

    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        # CPU 实测数学密集书 ~7s/页；超时按页数扩展（上限 2h），超时由上层兜底 PyMuPDF
        timeout_s = max(settings.mineru_timeout_s, min(n * 12, 7200))
        part = mineru_engine.parse(pdf_path, progress=lambda f, m: None, timeout_s=timeout_s)
    finally:
        stop.set()
    _merge_part(docir, part, None)
    docir["meta"].update({"parse_engine": "mineru",
                          "bbox_transform": part["meta"].get("bbox_transform", "none")})
    if not toc:   # 无书签：MinerU 标题块平铺
        toc = docir["toc"] = [{"level": 1, "title": b["text"], "page": b["page"]}
                              for b in part["blocks"] if b["type"] == "heading"]
    if toc:
        blocks_by_page: dict[int, list[dict]] = {}
        for b in docir["blocks"]:
            blocks_by_page.setdefault(b["page"], []).append(b)
        pipeline.backfill_sections(docir["blocks"], toc, blocks_by_page)
    if paper_id:
        _write(paper_id, docir)
    progress(0.8, f"MinerU 完成（耗时 {int(time.time() - t0)}s）· "
                  f"{len(docir['blocks'])} blocks 已落盘")


def _parse_batched_pymupdf(doc: fitz.Document, pdf_path: Path, decision: dict,
                           progress: ProgressCb, paper_id: str, docir: dict, toc: list[dict]) -> None:
    """PyMuPDF 逐批解析：50 页/批，批级进度与逐批落盘。"""
    n = doc.page_count
    total_batches = (n + BATCH_PAGES - 1) // BATCH_PAGES
    blocks_by_page: dict[int, list[dict]] = {}
    for bi in range(total_batches):
        start, end = bi * BATCH_PAGES, min((bi + 1) * BATCH_PAGES, n)
        b0, b1 = 0.08 + 0.72 * bi / total_batches, 0.08 + 0.72 * (bi + 1) / total_batches

        def batch_progress(frac: float, msg: str, _b0=b0, _b1=b1) -> None:
            progress(_b0 + (_b1 - _b0) * min(max(frac, 0.0), 1.0), msg)

        part = pipeline.parse_pdf(pdf_path, progress=batch_progress, pages=(start, end))
        _merge_part(docir, part, blocks_by_page)
        if not toc:   # 无书签：用已解析页的标题块渐进补齐 toc
            toc = docir["toc"] = pipeline._build_toc(doc, blocks_by_page)
        pipeline.backfill_sections(docir["blocks"], toc, blocks_by_page)
        if paper_id:
            _write(paper_id, docir)
        progress(b1, f"已解析 {end}/{n} 页（批次 {bi + 1}/{total_batches}）"
                     f" · {len(docir['blocks'])} blocks")


def _write(paper_id: str, docir: dict) -> None:
    DOCIR_DIR.mkdir(parents=True, exist_ok=True)
    (DOCIR_DIR / f"{paper_id}.json").write_text(
        _json_dumps(docir), encoding="utf-8")


def _json_dumps(docir: dict) -> str:
    import json
    return json.dumps(docir, ensure_ascii=False)


__all__ = ["BATCH_PAGES", "parse_long_pdf"]
