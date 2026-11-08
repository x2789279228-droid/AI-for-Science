"""解析质量自检（方案 docs/01 §4.3，评分表加分动作「解析报告」六指标）。

compute(docir, probe) → {"text_coverage", "toc_depth", "formula_count", "table_count",
"reference_parse_rate", "scanned_ratio", "overall"}：全部 0~1 或整数，
overall 为加权综合分（meta.parse_quality）。
"""
from __future__ import annotations

from typing import Any


def compute(docir: dict, probe: dict | None = None) -> dict[str, Any]:
    blocks = docir.get("blocks", [])
    toc = docir.get("toc", [])
    tables = docir.get("tables", [])
    refs = docir.get("references", [])
    meta = docir.get("meta", {})
    page_count = meta.get("page_count") or (probe or {}).get("page_count") or 0

    # ① 文本覆盖率：有内容块的页占比（文本层/OCR/救援/图后仍为空的页越少越好）
    pages_with_text = {b["page"] for b in blocks if (b.get("text") or "").strip()}
    pages_with_text |= {t["page"] for t in tables}
    pages_with_text |= {f["page"] for f in docir.get("figures", [])}
    text_coverage = (len(pages_with_text) / page_count) if page_count else 0.0

    # ② 标题层级深度（≥2 层为达标）
    toc_depth = max((t.get("level", 0) for t in toc), default=0)

    # ③ 公式识别数
    formula_count = sum(1 for b in blocks if b.get("type") == "formula")

    # ④ 表格数
    table_count = len(tables)

    # ⑤ 参考文献解析率：title 字段解析成功的占比（无参考文献章节 → None 不计入分）
    ref_rate = None
    if refs:
        ref_rate = round(sum(1 for r in refs if (r.get("title") or "").strip()) / len(refs), 3)

    # ⑥ 扫描/救援页占比
    scanned_ratio = round(len(meta.get("scanned_pages") or []) / page_count, 3) if page_count else 0.0

    overall = _overall(text_coverage, toc_depth, formula_count, table_count, ref_rate)
    return {"text_coverage": round(text_coverage, 3),
            "toc_depth": toc_depth,
            "formula_count": formula_count,
            "table_count": table_count,
            "reference_parse_rate": ref_rate,
            "scanned_ratio": scanned_ratio,
            "overall": round(overall, 3)}


def _overall(text_coverage: float, toc_depth: int, formula_count: int,
             table_count: int, ref_rate: float | None) -> float:
    """加权综合分：覆盖率 40% + 标题深度 20% + 结构化产物 30% + 参考文献解析 10%。"""
    score = 0.4 * min(1.0, text_coverage)
    score += 0.2 * min(1.0, toc_depth / 2.0)
    # 结构化产物：公式与表格各占一半（有一样给一半，两样齐全给满）
    score += 0.3 * (0.5 * (formula_count > 0) + 0.5 * (table_count > 0))
    if ref_rate is not None:
        score += 0.1 * ref_rate
    else:
        score += 0.1  # 无参考文献章节（书籍前言等）不惩罚
    return min(1.0, score)
