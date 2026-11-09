"""解析质量自检（方案 docs/01 §4.3，评分表加分动作「解析报告」六指标 + 细粒度 P0-7 正确性指标）。

compute(docir, probe) → {"text_coverage", "toc_depth", "heading_rate", "title_ok",
"formula_count", "table_count", "reference_parse_rate", "scanned_ratio", "overall"}：
全部 0~1 或整数，overall 为加权综合分（meta.parse_quality）。

P0-7 修订：旧 overall 只统计「产出存在性」（有公式/表格/标题层数），无法反映结构
正确性（v7 章节整页错配仍得 0.85 的实证）——新增 heading_rate（toc 标题→块命中率）
与 title_ok（标题非 stem 退化形），并把 ref_rate 收紧为 title 长度 ≥8 才算解析成功。

Round4 扩展：heading_outline_hit（PDF 书签↔解析 toc 页码对齐率，出版级真值探针，
无书签时 None 不惩罚）与 table_continuation（表头重复=跨页续表迹象，报告型不设门禁）。
"""
from __future__ import annotations

import re
from typing import Any

from .pipeline import _LEAD_NUM, _norm_title

_STEM_TITLE = re.compile(r"^(p_[0-9a-f]{6,}|seg_\d{2,}|fx_\w+)$", re.IGNORECASE)
_CITE_MARK = re.compile(r"\[(\d{1,3})\]")


def compute(docir: dict, probe: dict | None = None,
            outline: list[tuple[int, str, int]] | None = None) -> dict[str, Any]:
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

    # ②b heading 命中率（P0-7 正确性）：toc 标题能在 blocks 中找到的比例
    # （与 backfill 同口径：归一化全等 + 剥前导编号降级）——章节错配/标题块未识别时如实下降
    block_norms = {_norm_title(b.get("text", "")) for b in blocks if (b.get("text") or "").strip()}
    block_stripped = {_LEAD_NUM.sub("", bn, count=1) for bn in block_norms}
    toc_titles = [(t.get("title") or "").strip() for t in toc if (t.get("title") or "").strip()]
    hits = sum(1 for tt in toc_titles
               if (nt := _norm_title(tt)) in block_norms
               or _LEAD_NUM.sub("", nt, count=1) in block_stripped)
    heading_rate = (round(hits / len(toc_titles), 3) if toc_titles else None)

    # ②c title_ok（P0-7）：标题非空且非文件名退化形（p_xxx/seg_xxx/fx_*）
    title = (meta.get("title") or "").strip()
    title_ok = bool(title) and not _STEM_TITLE.match(title)

    # ③ 公式识别数
    formula_count = sum(1 for b in blocks if b.get("type") == "formula")

    # ④ 表格数
    table_count = len(tables)

    # ⑤ 参考文献解析率：title 长度 ≥8 才算解析成功（旧口径只看非空，"Le" 碎段虚高 100%）
    ref_rate = None
    if refs:
        ref_rate = round(sum(1 for r in refs if len((r.get("title") or "").strip()) >= 8)
                         / len(refs), 3)

    # ⑤b Phase 0：条数一致性 + 脏块暴露——分母取 max(条目编号, 正文引用标记 max [n])：
    # 只对比条目自身编号时"单条 [1] 巨块"仍满分（条数=编号=1），必须以正文实际引用
    # 到的编号为锚才能戳穿折叠；anomaly 单条超长计数为第二道探针。
    ref_consistency = None
    ref_anomaly = 0
    if refs:
        max_idx = max((r.get("index") or 0) for r in refs)
        body_max_cite = 0
        for b in blocks:
            body_max_cite = max(body_max_cite,
                                max((int(m) for m in _CITE_MARK.findall(b.get("text") or "")),
                                    default=0))
        denom = max(max_idx, body_max_cite)
        if denom > 0:
            ref_consistency = round(min(1.0, len(refs) / denom), 3)
        ref_anomaly = sum(1 for r in refs if r.get("anomaly"))

    # ⑥ 扫描/救援页占比
    scanned_ratio = round(len(meta.get("scanned_pages") or []) / page_count, 3) if page_count else 0.0

    # ⑥b Round4 正确性扩展：heading 与 PDF 书签（outline）的页码对齐率——书签是
    # 出版级真值，toc 命中且页差 ≤1 才算对齐（章节整页错配探针；无书签则 None 不惩罚）
    heading_outline_hit = None
    if outline:
        block_pages = {_norm_title(b.get("text", "")): b["page"]
                       for b in blocks if b.get("type") == "heading" and (b.get("text") or "").strip()}
        hits = 0
        for _, otitle, opage in outline:
            if not (otitle or "").strip() or not opage:
                continue
            nt = _LEAD_NUM.sub("", _norm_title(otitle), count=1)
            for bn, bp in block_pages.items():
                if bn == nt or _LEAD_NUM.sub("", bn, count=1) == nt:
                    hits += int(abs(bp - (opage - 1)) <= 1)   # outline 1 基 → blocks 0 基
                    break
        total = sum(1 for _, t, p in outline if (t or "").strip() and p)
        heading_outline_hit = round(hits / total, 3) if total else None

    # ⑥c Round4 报告型指标：表格续表迹象（markdown 中表头行重复出现 = 跨页/续表合并）
    table_continuation = sum(1 for t in tables if _has_repeated_header(t.get("markdown") or ""))

    overall = _overall(text_coverage, toc_depth, heading_rate, title_ok,
                       formula_count, table_count, ref_rate)
    return {"text_coverage": round(text_coverage, 3),
            "toc_depth": toc_depth,
            "heading_rate": heading_rate,
            "heading_outline_hit": heading_outline_hit,
            "title_ok": title_ok,
            "formula_count": formula_count,
            "table_count": table_count,
            "table_continuation": table_continuation,
            "reference_parse_rate": ref_rate,
            "ref_count_consistency": ref_consistency,
            "ref_anomaly": ref_anomaly,
            "scanned_ratio": scanned_ratio,
            "overall": round(overall, 3)}


def _has_repeated_header(md: str) -> bool:
    """表格 markdown 的表头行是否重复出现（跨页续表的典型形态）。报告型，不设门禁。"""
    rows = [r.strip() for r in md.splitlines() if r.strip().startswith("|")]
    if len(rows) < 3:
        return False
    header = rows[0]
    return any(r == header for r in rows[2:])   # 跳过分隔行 |---|


def _overall(text_coverage: float, toc_depth: int, heading_rate: float | None,
             title_ok: bool, formula_count: int, table_count: int,
             ref_rate: float | None) -> float:
    """加权综合分：覆盖率 35% + 标题深度 10% + heading 命中 15% + 结构化产物 20%
    + 文献解析 10% + title_ok 10%。toc/文献「不存在则不惩罚」（书籍前言等场景）。"""
    score = 0.35 * min(1.0, text_coverage)
    score += 0.10 * min(1.0, toc_depth / 2.0)
    score += 0.15 * (heading_rate if heading_rate is not None else 1.0)
    # 结构化产物：公式与表格各占一半（有一样给一半，两样齐全给满）
    score += 0.20 * (0.5 * (formula_count > 0) + 0.5 * (table_count > 0))
    score += 0.10 * (ref_rate if ref_rate is not None else 1.0)
    score += 0.10 * (1.0 if title_ok else 0.0)
    return min(1.0, score)
