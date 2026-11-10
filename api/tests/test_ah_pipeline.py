"""抗幻觉管线修订回归（P0）：L2 逐 support / L4 unchecked / 块级锚点 / 格级 E# / 冲突启发。"""
from __future__ import annotations

import uuid

from app.agents import nodes, tools
from app.rag import verify


def _ev(eid: int, text: str, paper_id: str = "p_a") -> dict:
    return {"chunk_id": f"c{eid}", "paper_id": paper_id, "text": text, "page": 3,
            "bbox": [0.1, 0.2, 0.9, 0.3], "section_path": "3.2 Attention",
            "block_ids": ["b10", "b11"], "kind": "detail", "score": 0.9}


def _sup(eid: str, quote: str, **extra) -> dict:
    s = {"eid": eid, "page": 4, "section_path": "3.2 Attention",
         "chunk_id": f"c{eid[1:]}", "quote": quote}
    s.update(extra)
    return s


# ---------- L2：逐 support 校验（P0 修订） ----------

def test_l2_checks_all_supports():
    """双 support、support[1] 的 quote 虚假 → unverified（旧实现只校验 support[0] 会漏）。"""
    eid_map = {1: _ev(1, "Multi-head attention attends to different subspaces at once."),
               2: _ev(2, "The model was trained on eight P100 GPUs for a long time."),
               3: _ev(3, "Unrelated filler text about physics.")}
    claims = [{"text": "MH attends subspaces; trained on P100.",
               "status": "supported",
               "support": [_sup("E1", "Multi-head attention attends to different subspaces"),
                           _sup("E2", "completely fabricated quote that is not in evidence")]}]
    out, guards = verify.check_quotes(claims, eid_map)
    c = out[0]
    assert c["quote_status"] == "unverified", "support[1] 虚假引文必须被抓（旧实现漏检）"
    assert c["quote_coverages"][0] >= 0.85 and c["quote_coverages"][1] < 0.85
    assert any(g.startswith("quote_unverified:") for g in guards)


def test_l2_verified_when_all_supports_match():
    eid_map = {1: _ev(1, "Multi-head attention attends to different subspaces at once."),
               2: _ev(2, "The model was trained on eight P100 GPUs.")}
    claims = [{"text": "MH subspaces; P100 GPUs.", "status": "supported",
               "support": [_sup("E1", "attends to different subspaces"),
                           _sup("E2", "trained on eight P100 GPUs")]}]
    out, _ = verify.check_quotes(claims, eid_map)
    assert out[0]["quote_status"] == "verified"
    assert min(out[0]["quote_coverages"]) >= 0.85


def test_l2_reextract_backfills_worst_support(monkeypatch):
    """重抽引文回填到覆盖率最低的 support（旧实现固定回填 support[0]）。"""
    eid_map = {1: _ev(1, "Alpha text about regularization and dropout schedules."),
               2: _ev(2, "Beta text about eight P100 GPUs training runs.")}
    claims = [{"text": "claim needing reextract.",
               "status": "supported",
               "support": [_sup("E1", "Alpha text about regularization and dropout"),
                           _sup("E2", "totally wrong fabrication quote")]}]
    monkeypatch.setattr(verify.llm, "available", lambda: True)
    captured = {}

    def fake_chat(messages, schema, retries=1, **kw):
        captured["called"] = True
        return {"quotes": {"claim needing reextract.": "eight P100 GPUs training runs"}}

    monkeypatch.setattr(verify.llm, "chat_json_validated", fake_chat)
    out, _ = verify.check_quotes(claims, eid_map)
    assert captured.get("called"), "失败后必须触发重抽"
    c = out[0]
    assert c["quote_status"] == "verified", "回填 worst support 后应转 verified"
    assert c["support"][1]["quote"] == "eight P100 GPUs training runs"


# ---------- L4：unchecked 诚实标注（P0 修订） ----------

def test_l4_off_marks_unchecked(monkeypatch):
    from app.config import settings
    eid_map = {1: _ev(1, "Alpha text.")}
    claims = [{"text": "c", "status": "supported", "support": [_sup("E1", "alpha quote")]}]
    monkeypatch.setattr(settings, "semantic_judge", False)
    out, guards = verify.judge_claims(claims, eid_map)
    assert out[0]["semantic_status"] == "unchecked", "关闭 L4 不得伪装绿色已验证"
    assert "semantic_judge_off" in guards


def test_l4_no_llm_marks_unchecked(monkeypatch):
    from app.config import settings
    eid_map = {1: _ev(1, "Alpha text.")}
    claims = [{"text": "c", "status": "supported", "support": [_sup("E1", "alpha quote")]}]
    monkeypatch.setattr(settings, "semantic_judge", True)
    monkeypatch.setattr(verify.llm, "available", lambda: False)
    out, guards = verify.judge_claims(claims, eid_map)
    assert out[0]["semantic_status"] == "unchecked"
    assert "semantic_judge_skipped" in guards


# ---------- 锚点精化：quote → 原句块 ----------

def test_refine_support_blocks_locates_quote_block(tmp_path):
    from app.config import DOCIR_DIR
    pid = f"p_blk_{uuid.uuid4().hex[:6]}"
    docir = {"blocks": [
        {"id": "b10", "type": "paragraph", "page": 3,
         "text": "Multi-head attention allows the model to jointly attend to information "
                 "from different representation subspaces at different positions.",
         "bbox": [0.08, 0.30, 0.92, 0.42]},
        {"id": "b11", "type": "paragraph", "page": 3,
         "text": "The Transformer employs stacked encoder and decoder layers with residual "
                 "connections and layer normalization between blocks.",
         "bbox": [0.08, 0.44, 0.92, 0.56]},
    ]}
    DOCIR_DIR.mkdir(parents=True, exist_ok=True)
    import json as _json
    (DOCIR_DIR / f"{pid}.json").write_text(_json.dumps(docir), encoding="utf-8")
    evidence = [_ev(1, docir["blocks"][0]["text"])]
    evidence[0]["paper_id"] = pid
    claims = [{"text": "MH joint attend.", "status": "supported", "support": [
        _sup("E1", "attend to information from different representation subspaces at different positions")]}]
    from app.rag.answering import refine_support_blocks
    n = refine_support_blocks(claims, evidence)
    s = claims[0]["support"][0]
    assert n == 1 and s.get("block_id") == "b10"
    assert s["block_bbox"] == [0.08, 0.30, 0.92, 0.42]


def test_refine_support_blocks_no_write_on_miss(tmp_path):
    from app.config import DOCIR_DIR
    pid = f"p_blk2_{uuid.uuid4().hex[:6]}"
    docir = {"blocks": [{"id": "b10", "type": "paragraph", "page": 3,
                         "text": "Alpha block text about dropout.", "bbox": [0.1, 0.1, 0.9, 0.2]}]}
    DOCIR_DIR.mkdir(parents=True, exist_ok=True)
    import json as _json
    (DOCIR_DIR / f"{pid}.json").write_text(_json.dumps(docir), encoding="utf-8")
    evidence = [dict(_ev(1, docir["blocks"][0]["text"]), paper_id=pid)]
    claims = [{"text": "c", "status": "supported",
               "support": [_sup("E1", "completely unrelated fabricated sentence here")]}]
    from app.rag.answering import refine_support_blocks
    n = refine_support_blocks(claims, evidence)
    assert n == 0 and "block_id" not in claims[0]["support"][0], "未命中不得写字段（诚实回退）"


# ---------- compare：格级 E# + 冲突启发 + 对称覆盖 ----------

def test_inject_compare_eids():
    hits = [_ev(1, "a"), _ev(2, "b")]
    state = {"compare_table": {"rows": [
        {"dimension": "d1", "cells": [
            {"paper_id": "p_a", "found": True, "chunk_id": "c1"},
            {"paper_id": "p_b", "found": True, "chunk_id": "c2"},
            {"paper_id": "p_c", "found": False}]}]}}
    table, n_conf = nodes._inject_compare_eids(state, hits)
    assert table["rows"][0]["cells"][0]["eid"] == "E1"
    assert table["rows"][0]["cells"][1]["eid"] == "E2"
    assert n_conf == 0


def test_align_dimension_conflict_heuristic():
    # Round5 注：p_b 证据须含维度特征词（BLEU）——否则被弱命中诚实降级为缺失格，
    # 单侧 found 不再触发冲突启发（旧夹具的 GLUE 文本现在会被正确降级）
    hits = [
        {"aspect": "BLEU", "paper_id": "p_a", "chunk_id": "c1", "rrf": 0.9,
         "text": "our model achieves 28.4 BLEU on WMT14"},
        {"aspect": "BLEU", "paper_id": "p_b", "chunk_id": "c2", "rrf": 0.8,
         "text": "the pretrained model reports 88.5 on BLEU-style eval"},
        {"aspect": "Layers", "paper_id": "p_a", "chunk_id": "c3", "rrf": 0.9,
         "text": "both models use 6 layers"},
        {"aspect": "Layers", "paper_id": "p_b", "chunk_id": "c4", "rrf": 0.8,
         "text": "the encoder stack also uses 6 layers"},
    ]
    out = tools._align_dimension(hits, ["p_a", "p_b"])
    by_dim = {r["dimension"]: r for r in out["rows"]}
    assert by_dim["BLEU"].get("conflict") is True, "数值集合不相交 → 冲突行"
    assert not by_dim["Layers"].get("conflict"), "数字集合有交集 → 不标冲突"


def test_compare_covered_papers():
    eid_map = {1: _ev(1, "a", paper_id="p_a"), 2: _ev(2, "b", paper_id="p_b")}
    claims = [{"text": "c1", "support": [_sup("E1", "q")]},
              {"text": "c2", "support": []}]
    covered = nodes._compare_covered_papers(claims, eid_map)
    assert covered == {"p_a"}
