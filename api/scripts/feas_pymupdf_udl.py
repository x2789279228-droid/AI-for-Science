"""可行性核实（一次性）：UDL 541 页用 PyMuPDF 快速路径直解的时长与质量。

对照：MinerU 路径实测 6843s（114min）、684MB RSS、toc=181、公式 0、537 页。
验证假设：text-native 长文档走 PyMuPDF ≤3min 且结构质量可用（toc/blocks/公式/双栏）。
密闭数据目录，不碰生产库；只调 pipeline.parse_pdf，不落 papers 表。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("MINERU_ENABLED", "off")
os.environ.setdefault("SCHOLAR_DATA_DIR",
                      str(Path(__file__).parent.parent / "tests" / f"_feas_{int(time.time())}"))

import psutil

from app.parsing import pipeline

PDF = Path(__file__).resolve().parents[3] / "downloads" / "udl" / "UnderstandingDeepLearning_02_09_26_C.pdf"
OUT = Path(__file__).parent / "feas_pymupdf_udl.json"

proc = psutil.Process()
t0 = time.time()
docir = pipeline.parse_pdf(PDF, progress=lambda f, m: print(f"  [{f:.2f}] {m}", flush=True))
elapsed = time.time() - t0
peak_mb = proc.memory_info().rss / 1048576

meta = docir["meta"]
headings = [b for b in docir["blocks"] if b.get("type") == "heading"]
formulas = [b for b in docir["blocks"] if b.get("type") == "formula"]
summary = {
    "elapsed_s": round(elapsed, 1),
    "within_3min": elapsed <= 180,
    "peak_rss_mb": round(peak_mb, 1),
    "page_count": meta.get("page_count"),
    "blocks": len(docir["blocks"]),
    "headings": len(headings),
    "toc_entries": len(docir.get("toc", [])),
    "toc_depth": max((t.get("level", 1) for t in docir.get("toc", [])), default=0),
    "formulas": len(formulas),
    "tables": len(docir.get("tables", [])),
    "figures": len(docir.get("figures", [])),
    "references": len(docir.get("references", [])),
    "engine": meta.get("parse_engine"),
    "parse_quality": meta.get("parse_quality"),
}
# 抽样：前 5 个一级标题（对照书目录常识：Introduction/Probabilities 等）
l1 = [t["title"] for t in docir.get("toc", []) if t.get("level") == 1][:8]
summary["toc_l1_sample"] = l1
OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
