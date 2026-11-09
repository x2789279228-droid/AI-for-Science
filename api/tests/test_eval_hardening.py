r"""Round4 评测硬化单元测试：严格匹配器 / rubric 真 8 问 / compare 金标 / 表块加权 / 跨块锚点。

背景（评审意见）：strict_recall_at5=0.6 的 4 个失败模式（\w 边界假阴性 ×2、真缺失 ×2）、
rubric8 实为 6 问、compare 无金标矩阵、anchor 只看首个 support。
"""
from __future__ import annotations

import sys
from pathlib import Path

# eval_gold 是 scripts 下的脚本（非包）：把 scripts 目录挂上 sys.path 后导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import eval_gold as eg

from app import db
from app.rag import answering, indexing

# ---------- 严格匹配器：alnum 边界 + 版式伪影归一 ----------

def test_strict_hit_alnum_boundary():
    """round3 假阴性复现：\\w 边界把 warmup_steps/8块 判词内 → alnum 边界修复。"""
    assert eg._strict_hit(("warmup",), "we used warmup_steps before decay")
    assert not eg._strict_hit(("28.4",), "we achieved 128.4 BLEU")   # 128.4 不误吞 28.4
    assert not eg._strict_hit(("6",), "65M parameters")              # 65 内的 6 不算
    assert eg._strict_hit(("6",), "batch size 6 块卡")               # 数字+中文量词合法


def test_strict_hit_parse_artifact_normalization():
    """版式伪影归一：数字间空格（"= 5 1 2"）与数字字母粘连（"6identical"）。"""
    assert eg._strict_hit(("512",), "d_model = 5 1 2 in the table")
    assert eg._strict_hit(("6",), "stack of N = 6identical layers")
    assert eg._norm_search_text("= 5 1 2") == "= 512"
    assert eg._norm_search_text("6identical") == "6 identical"
    assert eg._norm_search_text("28.4 BLEU") == "28.4 BLEU"          # 小数点/正常空白不动


def test_strict_hit_variant_sets():
    """多关键词集合：任一论文真实用语变体命中即算；集合全空缺才算缺失。"""
    assert eg._strict_hit(("length", "constant"), "a constant number of operations")
    assert not eg._strict_hit(("length", "constant"), "linear scaling with sequence")
    golds = {g for _, gs in eg._EN_FACTS for g in gs}
    assert "constant" in golds and "six" in golds  # round4 变体确实落位


# ---------- rubric 真 8 问 ----------

def test_rubric_is_true_eight():
    assert len(eg._RUBRIC_8) == 8
    modes = {s.get("mode", "single") for s in eg._RUBRIC_8}
    assert modes == {"single", "compare_q1", "gpt3"}
    assert all(callable(s["check"]) for s in eg._RUBRIC_8)
    # #7 断言双篇标签 + 双向/掩码要点；#8 断言 few-shot + 免微调要点
    q7 = eg._RUBRIC_8[6]["check"]
    assert q7("P1 编码器-解码器；P2 使用掩码语言模型 MLM，双向表示", {})
    assert not q7("两篇都是注意力模型", {})
    q8 = eg._RUBRIC_8[7]["check"]
    assert q8("few-shot 通过上下文示例学习，无需微调或梯度更新", {})
    assert not q8("few-shot 需要在下游任务上微调全部参数", {})


# ---------- compare 金标 ----------

def _table(dimension: str, q1: str, q2: str):
    return {"compare_table": {"rows": [{"dimension": dimension, "cells": [
        {"paper_id": "att", "found": True, "quote": q1},
        {"paper_id": "bert", "found": True, "quote": q2},
    ]}]}}


def test_compare_gold_structure():
    assert len(eg._COMPARE_GOLD) == 3
    cells = [c for g in eg._COMPARE_GOLD for c in g["cells"].values()]
    assert any(c["expect"] == "missing" for c in cells)          # 缺失格（BERT 无 WMT14 BLEU）
    assert any("qualifier" in c for c in eg._COMPARE_GOLD[2]["cells"].values())
    assert eg._COMPARE_GOLD[1]["fabrication_patterns"]           # 编造守卫正则在位


def test_eval_compare_gold_found_cells():
    g = eg._COMPARE_GOLD[0]
    final = {**_table("预训练目标与上下文表示",
                      "encoder-decoder autoregressive decoding",
                      "masked language model bidirectional"),
             "answer": "P1 是编码器-解码器、解码单向自回归；P2 是掩码语言模型（MLM）、双向表示。"}
    ev = eg._eval_compare_gold(g, final, ["P1", "P2"])
    assert ev["gold_pass"] == 2 and ev["gold_total"] == 2


def test_eval_compare_gold_missing_honest_and_fabricated():
    g = eg._COMPARE_GOLD[1]
    honest = {**_table("WMT 2014 英德 BLEU", "28.4 BLEU on WMT 2014 En-De", ""),
              "answer": "P1 在 WMT 2014 EN-DE 上取得 28.4 BLEU；P2（BERT）原文未报告该指标。"}
    honest["compare_table"]["rows"][0]["cells"][1]["found"] = False
    ev = eg._eval_compare_gold(g, honest, ["P1", "P2"])
    assert ev["missing_pass"] == 1 and not ev["fabrication"] and ev["gold_pass"] == 2

    fabricated = {**_table("WMT 2014 英德 BLEU", "28.4 BLEU on WMT 2014 En-De", ""),
                  "answer": "P1 是 28.4 BLEU；BERT 的 BLEU 为 27.8。"}
    ev2 = eg._eval_compare_gold(g, fabricated, ["P1", "P2"])
    assert ev2["fabrication"] and ev2["missing_pass"] == 0


def test_eval_compare_gold_qualifier_required():
    g = eg._COMPARE_GOLD[2]
    unqualified = {**_table("层数与参数量", "N=6 layers, 65M params",
                            "BERT has 12 layers and 110M parameters"),
                   "answer": "P1 为 6 层；P2 为 12 层 1.1 亿参数。"}   # 未说明 Base/Large
    ev = eg._eval_compare_gold(g, unqualified, ["P1", "P2"])
    passes = {c["label"]: c["pass"] for c in ev["gold_cells"]}
    assert passes["P1"] and not passes["P2"]

    qualified = {**unqualified, "answer": "P1 为 6 层；P2 BERT-Base 为 12 层/110M，Large 为 24 层/340M。"}
    ev2 = eg._eval_compare_gold(g, qualified, ["P1", "P2"])
    assert ev2["gold_pass"] == 2


def test_eval_compare_gold_dimension_row_missing_fails():
    g = eg._COMPARE_GOLD[0]
    final = {**_table("训练数据规模", "WMT 2014", "BooksCorpus + Wikipedia"),
             "answer": "P1 encoder-decoder autoregressive；P2 masked language model bidirectional"}
    ev = eg._eval_compare_gold(g, final, ["P1", "P2"])
    assert ev["gold_pass"] == 0    # 维度行整体缺失 → found 格全不通过


# ---------- 表块加权 ----------

def test_boost_table_hits_pure():
    hits = [{"chunk_id": "c1", "kind": "detail", "rrf": 0.030},
            {"chunk_id": "c2", "kind": "table", "rrf": 0.022},
            {"chunk_id": "c3", "kind": "detail", "rrf": 0.010}]
    out = indexing._boost_table_hits(hits)
    assert out[0]["chunk_id"] == "c2" and out[0]["table_boosted"]
    assert abs(out[0]["rrf"] - 0.055) < 1e-6
    assert not out[1].get("table_boosted")
    assert indexing._IDENT_TOKEN.search("What is the model dimension d_model?")
    assert not indexing._IDENT_TOKEN.search("How many GPUs were used?")


def test_hybrid_search_identifier_query_retrieves_table():
    """round3 真缺失复现：d_model=512 类问题 top-5 必须召回表格块（表块加权后）。"""
    db.init_db()
    pid = "p_tblboost"
    chunks = [
        {"text": "The dimension of the model is relatively small compared to prior work.",
         "page": 0, "bbox": [0, 0, 1, 1], "section_path": "s", "block_ids": ["b1"],
         "kind": "detail"},
        {"text": "| N | d_model | d_ff |\n|---|---|---|\n| 6 | 512 | 2048 |",
         "page": 1, "bbox": [0, 0, 1, 1], "section_path": "表1", "block_ids": ["t1"],
         "kind": "table"},
    ]
    indexing.index_chunks(pid, chunks)
    try:
        res = indexing.hybrid_search([pid], "What is the model dimension d_model?", top_k=2)
        hits = res["hits"]
        # 功能目标：d_model 类问题 top-k 召回含 512 的表格块，且该块被加权重排。
        # 绝对名次受 session 库其它语料的 FTS 语料统计影响（doc-freq 漂移），
        # 不在此断言——次序断言在 test_boost_table_hits_pure 纯函数中。
        kinds = [(h.get("kind"), h.get("table_boosted")) for h in hits]
        assert ("table", True) in kinds
        assert eg._strict_hit(("512",), " ".join(h.get("text") or "" for h in hits))
        # 非标识符查询零影响（不加权、不重排标记）
        res2 = indexing.hybrid_search([pid], "dimension of the model", top_k=2)
        assert all(not h.get("table_boosted") for h in res2["hits"])
    finally:
        with db.tx() as conn:
            conn.execute("DELETE FROM chunks WHERE paper_id=?", (pid,))
            conn.execute("DELETE FROM chunks_fts WHERE paper_id=?", (pid,))


# ---------- 跨块锚点（refine 第二轮：相邻块拼接） ----------

def test_refine_support_blocks_span_join(monkeypatch):
    docir = {"blocks": [
        {"id": "b1", "type": "paragraph", "text": "The decoder is autoregressive.",
         "page": 0, "bbox": [0.1, 0.1, 0.9, 0.2], "section_path": ""},
        {"id": "b2", "type": "paragraph", "text": "It generates one symbol at a time.",
         "page": 0, "bbox": [0.1, 0.2, 0.9, 0.3], "section_path": ""},
    ]}
    monkeypatch.setattr("app.rag.version_diff.load_docir_for", lambda pid: docir)
    quote = "The decoder is autoregressive. It generates one symbol at a time."
    claims = [{"quote_status": "verified",
               "support": [{"quote": quote, "chunk_id": "c1"}]}]
    evidence = [{"chunk_id": "c1", "paper_id": "p", "page": 0, "block_ids": []}]
    n = answering.refine_support_blocks(claims, evidence)
    assert n == 1
    s = claims[0]["support"][0]
    assert s["block_id"] == "b1" and s["block_ids"] == ["b1", "b2"]
    assert s["block_span"] is True
    assert s["block_bbox"] == [0.1, 0.1, 0.9, 0.3]   # 两块并集
