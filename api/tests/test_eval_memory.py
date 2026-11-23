"""模块七 P2：种子生成器 / 纵向金标评测的纯函数单测（不触库、零 app 导入）。

对应清单 §三#19/#20：故事线计划可复现 · 金标结构完备 · 20% 抽检比例 · P-R 与 hit@k 数学。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import eval_memory as ev
import seed_memory_demo as seeder

# ---------- 种子计划 ----------

def test_build_plan_deterministic():
    a = seeder.build_plan(30, 42)
    b = seeder.build_plan(30, 42)
    assert a == b, "同 seed 必须可复现"
    assert len(a) == 30


def test_build_plan_story_lines():
    plan = seeder.build_plan(30, 42)
    roles = {i + 1: [d["role"] for d in drafts] for i, drafts in enumerate(plan)}
    assert roles[2] == ["hyp"], "conv2 埋假设 A"
    assert roles[24] == ["ev_ref"], "conv24 埋矛盾证据"
    assert "hyp" in roles[5] and "hyp" in roles[7] and "hyp" in roles[9]
    assert "refine" in roles[20], "conv20 埋修正假设（取代线）"
    assert "ev_sup" in roles[8] and "ev_sup" in roles[12] and "ev_sup" in roles[14]
    noise_convs = [c for c in range(1, 31) if c not in {2, 5, 7, 8, 9, 12, 14, 20, 24}]
    assert all(roles[c] for c in noise_convs), "噪声会话撑干扰面"


def test_flatten_ordered_by_ts():
    plan = seeder.build_plan(30, 42)
    rows = seeder.flatten(plan, 30, 1000.0)
    ts_list = [r["ts"] for r in rows]
    assert ts_list == sorted(ts_list)
    assert all(r["conversation_id"].startswith("seed_c") for r in rows)


# ---------- 金标与抽检 ----------

def test_gold_structure_and_review_ratio():
    plan = seeder.build_plan(30, 42)
    rows = seeder.flatten(plan, 30, 1000.0)
    sample = seeder.review_sample(rows, ratio=0.2, seed=42)
    n_story = sum(1 for r in rows if r["role"] != "noise")
    # 抽检政策：故事线（验收关键）全抽 + 噪声补足到 ≥20%；故事线本身可能超过 20%
    assert len(sample) == max(round(len(rows) * 0.2), n_story)
    assert all(s["content"] for s in sample)
    story_idx = [s["idx"] for s in sample if rows[s["idx"]]["role"] != "noise"]
    assert len(story_idx) == n_story, "故事线事件必须全部进入抽检"


def test_gold_fields():
    plan = seeder.build_plan(30, 42)
    rows = seeder.flatten(plan, 30, 1000.0)
    ids = {"hyp_A": "id_a", "hyp_B": "id_b", "hyp_C": "id_c", "hyp_D": "id_d",
           "hyp_D2": "id_d2", "ev_ref": "id_ref", "ev_sup_A1": "id_s1",
           "ev_sup_A2": "id_s2", "ev_sup_B": "id_sb"}
    gold = seeder.build_gold(ids, len(rows), 30)
    assert gold["meta"]["human_review"]["ratio"] == 0.2
    assert gold["meta"]["human_review"]["status"] == "pending"
    assert len(gold["hypotheses"]) == 5
    verdicts = {g["role"]: g.get("expect_verdict") for g in gold["hypotheses"]}
    assert verdicts["A"] == "refuted" and verdicts["B"] == "supported"
    assert verdicts["C"] == "unverified" and verdicts["D"] is None
    assert gold["contradiction_pairs"][0]["hyp"] == "id_a"
    assert len(gold["staleness"]) == 3
    assert "brief_check_turn30" in gold


# ---------- 评测指标数学 ----------

def test_pr_counts():
    r = ev.pr_counts({"a", "b"}, {"a", "c"})
    assert r == {"tp": 1, "fp": 1, "fn": 1, "precision": 0.5, "recall": 0.5,
                 "precision_defined": True, "recall_defined": True}
    empty = ev.pr_counts(set(), set())
    assert empty["precision"] == 0.0 and empty["precision_defined"] is False
    assert empty["recall"] == 0.0 and empty["recall_defined"] is False
    perfect = ev.pr_counts({"a"}, {"a"})
    assert perfect["precision"] == 1.0 and perfect["recall"] == 1.0


def test_hit_at_k_and_avoid():
    ranked = ["e1", "e2", "e3", "e4", "e5", "e6"]
    assert ev.hit_at_k(["e2", "e9"], ranked, k=5) == 0.5
    assert ev.hit_at_k([], ranked) == 0.0
    assert ev.avoid_leak(["e1", "e9"], ranked) == 0.5
    assert ev.avoid_leak([], ranked) == 0.0
