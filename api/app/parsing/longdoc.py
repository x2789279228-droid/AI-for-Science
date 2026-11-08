"""长文档流式分批解析（docs/01 §4.2 长文档通道 + docs/05/07 降级矩阵）。

MinerU 引擎（批切页，docs/07 遗留项落地）：
  PyMuPDF 切 ~100 页/段 → 逐段送 MinerU（每段独立超时 min(段页数*12, 默认值)）→
  page_offset 合并 → 每段增量落盘（大纲先出不变，正文渐进可见、进度真实）→
  段间可取消（终止当前子进程）→ 单段失败该段 PyMuPDF 兜底（meta.fallback_segments
  如实记录，不冒充）。整本单次模式保留（mineru_chunk_pages=0 关闭批切）。
PyMuPDF 引擎：50 页/批页范围直解析，批级进度细粒度可见。
目录先出：书签 toc 骨架先落盘（秒级），reader 立即可见大纲，正文随后追加。
索引降级（前 N 页全文 chunk）由 jobs 层按 meta.long_doc 处理。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import fitz  # PyMuPDF

from ..config import DOCIR_DIR, settings
from . import mineru_engine, pipeline, vlm_engine
from ._types import CancelCheck, ParseCancelled, ProgressCb

BATCH_PAGES = 50   # docs/05：每 50 页一个批次（PyMuPDF 通道；进度/降级叙事单位）


def parse_long_pdf(pdf_path: Path, probe: dict, decision: dict,
                   progress: ProgressCb, paper_id: str = "",
                   cancel_check: CancelCheck | None = None) -> dict:
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

    # ---- 阶段 2：正文解析（MinerU 批切页/整本 或 PyMuPDF 逐批） ----
    try:
        if decision["engine"] == "mineru" and mineru_engine.available():
            if settings.mineru_chunk_pages > 0:
                _parse_chunked_mineru(doc, pdf_path, decision, progress, paper_id,
                                      docir, toc, cancel_check)
            else:
                _parse_whole_mineru(doc, pdf_path, decision, progress, paper_id,
                                    docir, toc, cancel_check)
        else:
            _parse_batched_pymupdf(doc, pdf_path, decision, progress, paper_id,
                                   docir, toc, cancel_check)
    finally:
        doc.close()

    # ---- 阶段 3：灰区/扫描救援（空白页 → VLM，只对失败页调用） ----
    if decision["mode"] == "gray" and vlm_engine.available():
        docir = vlm_engine.rescue_blank_pages(pdf_path, docir, progress,
                                              cancel_check=cancel_check)
        if docir["meta"].get("rescued_pages"):
            docir["meta"]["parse_engine"] = "mixed"

    progress(0.85, f"长文档解析完成：{len(docir['blocks'])} blocks / "
                   f"{len(docir['tables'])} tables / toc {len(docir['toc'])} 条")
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


def _seg_progress(progress: ProgressCb, bi: int, total: int, base: float, span: float) -> None:
    b0 = base + span * bi / total
    b1 = base + span * (bi + 1) / total
    return lambda frac, msg, _b0=b0, _b1=b1: progress(
        _b0 + (_b1 - _b0) * min(max(frac, 0.0), 1.0), msg)


def _parse_chunked_mineru(doc: fitz.Document, pdf_path: Path, decision: dict,
                          progress: ProgressCb, paper_id: str, docir: dict,
                          toc: list[dict], cancel_check: CancelCheck | None) -> None:
    """MinerU 批切页：段级子进程 + 增量落盘 + 段级 PyMuPDF 兜底。

    为什么批切：MinerU CPU 实测 ~7s/页，整本 541 页必然撞单次超时防线（6492s 实录）；
    批切后每段独立超时、进度按段真实推进，且单段失败只损失该段（该段 PyMuPDF 兜底）。
    """
    n = doc.page_count
    seg_pages = max(settings.mineru_chunk_pages, 10)
    total = (n + seg_pages - 1) // seg_pages
    tmp_root = DOCIR_DIR.parent / "parsing" / f"chunks_{pdf_path.stem}_{int(time.time())}"
    tmp_root.mkdir(parents=True, exist_ok=True)
    seg_paths: list[Path] = []
    fallback_segments: list[dict] = []
    blocks_by_page: dict[int, list[dict]] = {}
    t0 = time.time()
    try:
        # 切段（PyMuPDF insert_pdf 逐段导出；内存按段有界）
        for si in range(total):
            start, end = si * seg_pages, min((si + 1) * seg_pages, n)
            seg = fitz.open()
            seg.insert_pdf(doc, from_page=start, to_page=end - 1)
            seg_path = tmp_root / f"seg_{si:03d}.pdf"
            seg.save(str(seg_path))
            seg.close()
            seg_paths.append(seg_path)

        for si, seg_path in enumerate(seg_paths):
            start, end = si * seg_pages, min((si + 1) * seg_pages, n)
            if cancel_check is not None and cancel_check():
                raise ParseCancelled(f"批切页解析已取消（第 {start} 页段边界）")
            prog = _seg_progress(progress, si, total, 0.08, 0.72)
            prog(0.0, f"MinerU 解析第 {start + 1}-{end} 页（段 {si + 1}/{total}）")
            timeout_s = max(300, min((end - start) * 12, settings.mineru_timeout_s))
            try:
                part = mineru_engine.parse(seg_path, progress=prog,
                                           page_offset=start, out_dir=tmp_root / f"out_{si:03d}",
                                           timeout_s=timeout_s, cancel_check=cancel_check)
                if not docir["meta"].get("bbox_transform"):
                    docir["meta"]["bbox_transform"] = part["meta"].get("bbox_transform", "none")
            except ParseCancelled:
                raise
            except Exception as e:  # noqa: BLE001 —— 单段失败该段降级，不拖垮全书
                fallback_segments.append({"start": start, "end": end,
                                          "reason": f"mineru:{type(e).__name__}"})
                prog(0.3, f"段 {si + 1} MinerU 失败（{type(e).__name__}）→ PyMuPDF 兜底")
                part = pipeline.parse_pdf(pdf_path, progress=prog, pages=(start, end),
                                          cancel_check=cancel_check)
            _merge_part(docir, part, blocks_by_page)
            if not toc:   # 无书签：用已解析页标题渐进补齐
                toc = docir["toc"] = pipeline._build_toc(doc, blocks_by_page)
            pipeline.backfill_sections(docir["blocks"], toc, blocks_by_page)
            if paper_id:
                _write(paper_id, docir)   # 段级增量落盘：reader 中途即可读已解析页
            progress(0.08 + 0.72 * (si + 1) / total,
                     f"已解析 {end}/{n} 页（段 {si + 1}/{total}）· {len(docir['blocks'])} blocks")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)   # 临时段与 MinerU 输出清理

    if fallback_segments:
        docir["meta"]["fallback_segments"] = fallback_segments
        ok = total - len(fallback_segments)
        docir["meta"]["parse_engine"] = "mineru-chunked" if ok == total else "mixed"
        docir["meta"]["chunked"] = {"segments": total, "mineru_ok": ok}
    else:
        docir["meta"]["parse_engine"] = "mineru-chunked"
        docir["meta"]["chunked"] = {"segments": total, "mineru_ok": total}
    if not toc:
        toc = docir["toc"] = [{"level": 1, "title": b["text"], "page": b["page"]}
                              for b in docir["blocks"] if b["type"] == "heading"]
    if paper_id:
        _write(paper_id, docir)
    note = f"，{len(fallback_segments)} 段兜底" if fallback_segments else ""
    progress(0.8, f"MinerU 批切完成（{total} 段 / 耗时 {int(time.time() - t0)}s{note}）· "
                  f"{len(docir['blocks'])} blocks 已落盘")


def _parse_whole_mineru(doc: fitz.Document, pdf_path: Path, decision: dict,
                        progress: ProgressCb, paper_id: str, docir: dict, toc: list[dict],
                        cancel_check: CancelCheck | None) -> None:
    """MinerU 整本单次调用（批切关闭时的兼容路径）：模型只加载一次；心跳保进度可见。"""
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
        part = mineru_engine.parse(pdf_path, progress=lambda f, m: None,
                                   timeout_s=timeout_s, cancel_check=cancel_check)
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
                           progress: ProgressCb, paper_id: str, docir: dict,
                           toc: list[dict], cancel_check: CancelCheck | None) -> None:
    """PyMuPDF 逐批解析：50 页/批，批级进度与逐批落盘。"""
    n = doc.page_count
    total_batches = (n + BATCH_PAGES - 1) // BATCH_PAGES
    blocks_by_page: dict[int, list[dict]] = {}
    for bi in range(total_batches):
        if cancel_check is not None and cancel_check():
            raise ParseCancelled(f"批解析已取消（第 {bi * BATCH_PAGES} 页批边界）")
        start, end = bi * BATCH_PAGES, min((bi + 1) * BATCH_PAGES, n)
        prog = _seg_progress(progress, bi, total_batches, 0.08, 0.72)
        part = pipeline.parse_pdf(pdf_path, progress=prog, pages=(start, end),
                                  cancel_check=cancel_check)
        _merge_part(docir, part, blocks_by_page)
        if not toc:   # 无书签：用已解析页的标题块渐进补齐 toc
            toc = docir["toc"] = pipeline._build_toc(doc, blocks_by_page)
        pipeline.backfill_sections(docir["blocks"], toc, blocks_by_page)
        if paper_id:
            _write(paper_id, docir)
        progress(0.08 + 0.72 * (bi + 1) / total_batches,
                 f"已解析 {end}/{n} 页（批次 {bi + 1}/{total_batches}）"
                 f" · {len(docir['blocks'])} blocks")


def _write(paper_id: str, docir: dict) -> None:
    DOCIR_DIR.mkdir(parents=True, exist_ok=True)
    (DOCIR_DIR / f"{paper_id}.json").write_text(
        json.dumps(docir, ensure_ascii=False), encoding="utf-8")


__all__ = ["BATCH_PAGES", "parse_long_pdf"]
