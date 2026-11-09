"""Round4 模块一测试：VLM 表/图 bbox 定位（不再整页占位）、质量报告扩展指标、
data-real 语料 references 健康度回归。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.parsing.quality import _has_repeated_header, compute
from app.parsing.vlm_engine import bbox_stats, markdown_to_docir

REAL_DOCIR = Path(__file__).resolve().parents[2] / "data-real" / "docir"


# ---------- VLM 表格/图 bbox（Round4：不再硬编码整页占位） ----------

def test_markdown_to_docir_table_figure_bbox():
    """块首 <<bbox>> 前缀：表格/图注用实值且无 estimated 标记；无标记诚实降级。"""
    md = (
        "<<bbox:100,150,900,400>>| Model | BLEU |\n|---|---|\n| Base | 27.3 |\n"
        "<<bbox:50,500,950,600>>[图] Figure 1: architecture\n"
        "# Untagged Heading\n"
        "| Plain | Tbl |\n|---|---|\n| a | 1 |\n"
        "[图] Figure 2: plain caption\n"
    )
    out = markdown_to_docir(md, 0)
    t1, t2 = out["tables"]
    assert t1["bbox"] == [0.1, 0.15, 0.9, 0.4] and "bbox_estimated" not in t1
    assert t1["headers"] == ["Model", "BLEU"]          # 前缀剥离后表头解析正常
    assert t2["bbox"] == [0.0, 0.0, 1.0, 1.0] and t2.get("bbox_estimated") is True
    f1, f2 = out["figures"]
    assert f1["bbox"] == [0.05, 0.5, 0.95, 0.6] and "bbox_estimated" not in f1
    assert f1["caption"].startswith("Figure 1")
    assert f2["bbox"] == [0.0, 0.0, 1.0, 1.0] and f2.get("bbox_estimated") is True
    # 非表格/图注的 bbox 前缀行不受表格分支误吞（标题仍正常识别）
    assert any(b["type"] == "heading" and b["text"] == "Untagged Heading"
               for b in out["blocks"])


def test_bbox_stats_counts():
    items = [{"bbox": [0, 0, 1, 1]}, {"bbox": [0, 0, 1, 1], "bbox_estimated": True},
             {"bbox": [0.1, 0.1, 0.5, 0.5]}]
    assert bbox_stats(items) == {"grounded": 2, "estimated": 1}


# ---------- 质量报告扩展：heading_outline_hit / table_continuation ----------

def _docir_fixture():
    return {
        "meta": {"title": "T", "page_count": 3},
        "toc": [{"level": 2, "title": "Introduction", "page": 0}],
        "blocks": [
            {"id": "b1", "type": "heading", "text": "1 Introduction", "page": 0, "bbox": []},
            {"id": "b2", "type": "heading", "text": "2 Methods", "page": 2, "bbox": []},
        ],
        "tables": [{"id": "t1", "page": 1,
                    "markdown": "| a | b |\n|---|---|\n| 1 | 2 |\n| a | b |\n| 3 | 4 |",
                    "bbox": []}],
        "figures": [], "references": [],
    }


def test_quality_heading_outline_hit():
    docir = _docir_fixture()
    # outline 1 基页码：Introduction→1（block 0，差 0）· Methods→4（block 2，差 1）全对齐
    q = compute(docir, {"page_count": 3},
                outline=[(1, "Introduction", 1), (1, "Methods", 4)])
    assert q["heading_outline_hit"] == 1.0
    # Methods 书签指到第 9 页（差 6）→ 未对齐
    q2 = compute(docir, {"page_count": 3}, outline=[(1, "Methods", 9)])
    assert q2["heading_outline_hit"] == 0.0
    # 无书签 → None（不惩罚）
    assert compute(docir, {"page_count": 3})["heading_outline_hit"] is None


def test_quality_table_continuation():
    assert compute(_docir_fixture(), {"page_count": 3})["table_continuation"] == 1
    assert _has_repeated_header("| a | b |\n|---|---|\n| 1 | 2 |") is False


# ---------- data-real 语料 references 健康度回归（快照在本机才跑） ----------

@pytest.mark.skipif(not REAL_DOCIR.exists(), reason="data-real 快照不在本机（fetch_corpus --snapshot 生成）")
def test_data_real_refs_health():
    """round3 基线：6 篇 refs=40/43/56/101/118/245，全 titled、anomaly<10%。

    快照 raw 截断到 500 字符（防脏巨块在落盘侧即被截），故 mega-block 断言口径
    为 ≤520（快照格式不变式）；条数下限取最小基线 40×0.95。
    """
    files = sorted(REAL_DOCIR.glob("*.json"))
    assert len(files) >= 6, "data-real 语料应 ≥6 篇"
    total = 0
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        refs = d.get("references", [])
        assert len(refs) >= 38, f"{f.name}: refs={len(refs)} < 38"
        total += len(refs)
        assert all(len(r.get("raw") or r.get("text") or "") <= 520
                   for r in refs), f"{f.name}: 存在 >520 字符条目（快照格式被破坏）"
        titled = sum(1 for r in refs if (r.get("title") or "").strip())
        assert titled / len(refs) >= 0.95, f"{f.name}: titled {titled}/{len(refs)}"
        anom = sum(1 for r in refs if r.get("anomaly"))
        assert anom / len(refs) < 0.10, f"{f.name}: anomaly {anom}/{len(refs)}"
    assert total >= 580, f"refs 总数 {total} < 580（语料退化）"
