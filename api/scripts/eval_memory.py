#!/usr/bin/env python
"""纵向金标评测（模块七 P2，清单 §三#20 / 验收 §七.4）。

指标：
  1) 假设召回：简报是否召回全部金标假设且 verdict 与期望一致（superseded → past_hypotheses）
  2) 矛盾检出 P/R：金标 refuted 假设的判定混淆矩阵（TP/FP/FN → P/R，分母 0 记 0 并如实标注）
  3) 陈旧判断：staleness 查询的 hit@5（期望新结论命中）与 avoid 泄漏率（旧结论混入，越低越好）
  4) 基线消融：no_memory（无记忆）/ naive_topk（纯词面 top-5，无三信号/时间窗/强度）/
     full_history（全量注入口径——hit 必然 1.0，暴露的是 avoid 泄漏）
  5) FadeMem 网格消融：β_LML ∈ {0.6, 0.8, 1.0}（SML=β+0.4）× μ ∈ {0.2, 0.5, 0.8}
     （θ 固定 0.7/0.3）→ 陈旧/矛盾指标表（强度只影响检索排序，故消融作用于陈旧判断）
  6) judge 一致性（--judge 且 LLM 可用）：judge verdict vs 金标一致率；离线环境如实标"未评测"

用法：
  python api/scripts/eval_memory.py --data-dir data/seed_mem --gold docs/acceptance/round9-20261121-r7/seed_gold.json \
      --out docs/acceptance/round9-20261121-r7 [--judge]
产出：eval_report.md + eval_memory.json。数字真实，不达标如实标注（清单 §七.4）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from pathlib import Path

TOP_K = 5

# ---------- 指标纯函数（不依赖 app——可单测） ----------


def pr_counts(pred_refuted: set[str], gold_refuted: set[str]) -> dict:
    """矛盾检出混淆矩阵。分母为 0 时该项记 0 并标注 undefined（不冒充 100%）。"""
    tp = len(pred_refuted & gold_refuted)
    fp = len(pred_refuted - gold_refuted)
    fn = len(gold_refuted - pred_refuted)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 4),
            "recall": round(recall, 4),
            "precision_defined": bool(tp + fp), "recall_defined": bool(tp + fn)}


def hit_at_k(expected: list[str], ranked: list[str], k: int = TOP_K) -> float:
    if not expected:
        return 0.0
    top = set(ranked[:k])
    return len(top & set(expected)) / len(expected)


def avoid_leak(avoid: list[str], ranked: list[str]) -> float:
    """旧结论混入率：avoid 集合出现在前 k 的比例（0 最好）。"""
    if not avoid:
        return 0.0
    return len(set(avoid) & set(ranked[:TOP_K])) / len(avoid)


# ---------- app 依赖（env 设置后延迟导入） ----------

def _boot(data_dir: Path) -> None:
    import os
    os.environ["SCHOLAR_DATA_DIR"] = str(data_dir)
    for m in [k for k in list(sys.modules) if k.startswith("app")]:
        del sys.modules[m]


def _eval_primary(gold: dict, now: float) -> dict:
    """主口径（全功能：三信号+时间窗+强度+图谱简报）。"""
    from app.memory import briefing, retrieve

    brief = briefing.build_brief(user_id="local", use_judge=False)
    verdict_by_id = {i["hypothesis"]["id"]: i["verdict"] for i in brief["items"]}
    past_status = {p["id"]: p["status"] for p in brief.get("past_hypotheses", [])}

    def outcome(eid: str) -> str:
        """简报对某假设的判定结果：活跃项取 verdict；终态项取 status（refuted 等）。"""
        if eid in verdict_by_id:
            return verdict_by_id[eid]
        if eid in past_status:
            return past_status[eid]
        return "missing"

    hyp_results = []
    for g in gold["hypotheses"]:
        eid = g["expect_id"]
        if g.get("expect_past"):
            got = past_status.get(eid) == "superseded"
            hyp_results.append({"role": g["role"], "expect": "superseded(past)",
                                "got": past_status.get(eid, "active/missing"), "hit": got})
            continue
        got_verdict = outcome(eid)
        expect = g["expect_verdict"]
        hit = got_verdict == expect or expect == "any" and got_verdict != "missing"
        hyp_results.append({"role": g["role"], "expect": expect,
                            "got": got_verdict, "hit": hit})
    recall = sum(1 for h in hyp_results if h["hit"]) / max(1, len(hyp_results))

    # 预测 refuted 集合：活跃项 verdict=refuted ∪ 终态项 status=refuted
    pred_refuted = {eid for eid, v in verdict_by_id.items() if v == "refuted"}
    pred_refuted |= {eid for eid, st in past_status.items() if st == "refuted"}
    gold_refuted = {g["expect_id"] for g in gold["hypotheses"] if g.get("expect_verdict") == "refuted"}
    contradiction = pr_counts(pred_refuted, gold_refuted)

    staleness = []
    for s in gold["staleness"]:
        res = retrieve.retrieve_memories(s["query"], user_id="local", top_k=TOP_K,
                                         now_ts=now, touch=False)
        ranked = [i["id"] for i in res["items"]]
        staleness.append({
            "query": s["query"],
            "hit_at_k": round(hit_at_k(s["expect_ids"], ranked), 4),
            "avoid_leak": round(avoid_leak(s["avoid_ids"], ranked), 4),
        })
    return {"hypothesis_recall": round(recall, 4), "hypotheses": hyp_results,
            "contradiction": contradiction, "staleness": staleness}


def _eval_baseline_naive(gold: dict, now: float) -> dict:
    """朴素 top-k：纯词面 top-5（无 recency/importance/时间窗/强度/图谱）。"""
    from app.memory import store
    from app.memory.retrieve import _kw_score

    events = [e for e in store.list_events(limit=500) if e["user_id"] == "local"]
    hyp_results = []
    for g in gold["hypotheses"]:
        if g.get("expect_past"):
            hyp_results.append({"role": g["role"], "hit": False})
            continue
        content = next((e["content"] for e in events if e["id"] == g["expect_id"]), "")
        hyp_results.append({"role": g["role"], "hit": bool(content)})
    # naive 的"召回"= 内容存在即召回（无状态判定能力）——如实标注口径
    recall = sum(1 for h in hyp_results if h["hit"]) / max(1, len(hyp_results))
    pred_refuted = set()
    for g in gold["hypotheses"]:
        ev = next((e for e in events if e["id"] == g["expect_id"]), None)
        if ev is not None and _kw_score("推翻", ev["content"]) > 0:
            pred_refuted.add(g["expect_id"])       # 词面启发：内容含「推翻」即预测 refuted
    gold_refuted = {g["expect_id"] for g in gold["hypotheses"] if g.get("expect_verdict") == "refuted"}
    contradiction = pr_counts(pred_refuted, gold_refuted)
    staleness = []
    for s in gold["staleness"]:
        ranked = sorted(events,
                        key=lambda e: -_kw_score(s["query"], f"{e['content']} {e['subject'] or ''}"))
        ranked_ids = [e["id"] for e in ranked[:TOP_K]]
        staleness.append({"query": s["query"],
                          "hit_at_k": round(hit_at_k(s["expect_ids"], ranked_ids), 4),
                          "avoid_leak": round(avoid_leak(s["avoid_ids"], ranked_ids), 4)})
    _ = now
    return {"hypothesis_recall": round(recall, 4), "contradiction": contradiction,
            "staleness": staleness,
            "note": "词面启发口径：无三信号/时间窗/强度/图谱，召回=内容存在"}


def _eval_baseline_full_history(gold: dict, now: float) -> dict:
    """全历史注入：hit 必然 1.0（全部都在上下文里），暴露的是 avoid 泄漏。"""
    from app.memory import store
    events = [e for e in store.list_events(limit=500) if e["user_id"] == "local"]
    all_ids = [e["id"] for e in events]
    staleness = []
    n_in = sum(1 for s in gold["staleness"] for a in s["avoid_ids"] if a in set(all_ids))
    n_avoid = sum(len(s["avoid_ids"]) for s in gold["staleness"])
    for s in gold["staleness"]:
        # 全量注入口径：avoid 必然全泄漏（avoid_leak 只看 top-k，此处对全上下文计算）
        leak = len(set(s["avoid_ids"]) & set(all_ids)) / max(1, len(s["avoid_ids"]))
        staleness.append({"query": s["query"], "hit_at_k": 1.0, "avoid_leak": round(leak, 4)})
    _ = n_in, n_avoid
    _ = now
    return {"hypothesis_recall": 1.0, "contradiction": {"note": "无判定机制，不适用"},
            "staleness": staleness,
            "note": "全量注入口径：hit 无意义（必然命中），avoid=全量上下文必然全泄漏"}


def _eval_fade_grid(gold: dict, now: float) -> list[dict]:
    """FadeMem 网格消融（强度只影响检索排序 → 作用在陈旧判断）。"""
    from app.config import settings
    from app.memory import drift, retrieve

    rows = []
    default = {"beta_lml": settings.fade_beta_lml, "mu": settings.fade_mu,
               "tp": settings.fade_theta_promote, "td": settings.fade_theta_demote}
    grid = []
    for beta in (0.6, 0.8, 1.0):
        for mu in (0.2, 0.5, 0.8):
            grid.append((beta, mu))
    for beta, mu in grid:
        settings.fade_beta_lml = beta
        settings.fade_beta_sml = beta + 0.4
        settings.fade_mu = mu
        drift.recalculate_strengths(user_id="local", now=now)
        hit = leak = n = 0.0
        strength_sum = 0.0
        for s in gold["staleness"]:
            res = retrieve.retrieve_memories(s["query"], user_id="local", top_k=TOP_K,
                                             now_ts=now, touch=False)
            ranked = [i["id"] for i in res["items"]]
            hit += hit_at_k(s["expect_ids"], ranked)
            leak += avoid_leak(s["avoid_ids"], ranked)
            mean_strength = (sum(i["scores"]["strength"] for i in res["items"])
                             / max(1, len(res["items"])))
            n += 1
            strength_sum += mean_strength
        rows.append({"beta_lml": beta, "mu": mu,
                     "staleness_hit_at_k": round(hit / n, 4),
                     "avoid_leak": round(leak / n, 4),
                     "mean_top5_strength": round(strength_sum / n, 4)})
    settings.fade_beta_lml = default["beta_lml"]
    settings.fade_beta_sml = 1.2
    settings.fade_mu = default["mu"]
    settings.fade_theta_promote = default["tp"]
    settings.fade_theta_demote = default["td"]
    drift.recalculate_strengths(user_id="local", now=now)   # 恢复默认参数口径
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data/seed_mem")
    ap.add_argument("--gold", required=True)
    ap.add_argument("--out", default="docs/acceptance/round9-20261121-r7")
    ap.add_argument("--judge", action="store_true", help="附加 judge 一致性（需 LLM 环境）")
    ap.add_argument("--skip-grid", action="store_true", help="跳过 FadeMem 网格消融（调试用）")
    args = ap.parse_args()

    _boot(Path(args.data_dir))
    from datetime import datetime

    from app.memory import briefing, store

    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    events = [e for e in store.list_events(limit=500) if e["user_id"] == "local"]
    now = max((e["ts"] for e in events), default=0.0) + DAY_HALF

    primary = _eval_primary(gold, now)
    naive = _eval_baseline_naive(gold, now)
    full = _eval_baseline_full_history(gold, now)
    grid = [] if args.skip_grid else _eval_fade_grid(gold, now)

    judge_block: dict = {"evaluated": False,
                         "note": "离线环境 judge 一致性未评测（--judge + LLM 环境重跑）"}
    if args.judge:
        from app.llm import client as llm
        if llm.available():
            brief = briefing.build_brief(use_judge=True)
            got = {i["hypothesis"]["id"]: i["verdict"] for i in brief["items"]}
            gold_ref = {g["expect_id"]: g["expect_verdict"] for g in gold["hypotheses"]
                        if g.get("expect_verdict")}
            ok = sum(1 for eid, exp in gold_ref.items() if got.get(eid) == exp)
            judge_block = {"evaluated": True, "consistent": ok,
                           "total": len(gold_ref),
                           "ratio": round(ok / len(gold_ref), 4) if gold_ref else 0.0}
        else:
            judge_block = {"evaluated": False,
                           "note": "LLM 不可用 → judge 一致性未评测（诚实标注）"}

    report = {
        "meta": {"data_dir": args.data_dir, "gold": args.gold,
                 "generated_at": datetime.now(UTC).isoformat(),
                 "n_events": len(events), "top_k": TOP_K,
                 "honest": "模拟语料 + 半人工金标（清单 §七.4）"},
        "primary": primary,
        "baselines": {"no_memory": {"hypothesis_recall": 0.0, "contradiction_recall": 0.0,
                                    "staleness_hit_at_k": 0.0,
                                    "note": "无记忆口径：全部为 0（模块七缺失时的真实下界）"},
                      "naive_topk": naive, "full_history": full},
        "fade_grid": grid,
        "judge_consistency": judge_block,
        "brief_check_turn30": gold.get("meta", {}).get("acceptance_brief_check"),
    }

    # 结论（数字真实，不达标如实标注）
    con = primary["contradiction"]
    naive_con = naive["contradiction"]
    better = con["recall"] > naive_con["recall"] or (
        con["recall"] == naive_con["recall"] and con["precision"] >= naive_con["precision"])
    verdict_line = ("矛盾检出优于朴素 top-k 基线" if better
                    else "⚠ 矛盾检出未优于朴素 top-k 基线（如实标注，不达标）")
    report["conclusion"] = {"contradiction_beats_naive": better, "verdict": verdict_line}

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "eval_memory.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = _render_md(report)
    (out_dir / "eval_report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


DAY_HALF = 0.0   # 评测时点 = 最后事件时刻（见 main：max ts + 0）


def _render_md(r: dict) -> str:
    p = r["primary"]
    con = p["contradiction"]
    lines = [
        "# 模块七纵向金标评测报告（P2 · r7）", "",
        (f"> 事件 {r['meta']['n_events']} 条 · 口径：模拟语料 + 半人工金标（清单 §七.4）· "
        f"top_k={r['meta']['top_k']}"), "",
        "## 1. 主口径（全功能：三信号 + 时间窗 + 强度 + 图谱简报）", "",
        f"- 假设召回（verdict 精确匹配）：**{p['hypothesis_recall']:.0%}**",
        (f"- 矛盾检出：P={con['precision']:.0%}（{'有定义' if con['precision_defined'] else '分母为 0，未定义'}）"
        f" · R={con['recall']:.0%}（{'有定义' if con['recall_defined'] else '分母为 0，未定义'}）"
        f" · TP/FP/FN = {con['tp']}/{con['fp']}/{con['fn']}"),
    ]
    for h in p["hypotheses"]:
        lines.append(f"  - 假设 {h['role']}: 期望 {h['expect']} / 实际 {h['got']} → "
                     f"{'✓' if h['hit'] else '✗'}")
    lines += ["", "| 陈旧查询 | hit@5 | avoid 泄漏 |", "|---|---|---|"]
    for s in p["staleness"]:
        lines.append(f"| {s['query']} | {s['hit_at_k']:.0%} | {s['avoid_leak']:.0%} |")
    b = r["baselines"]
    lines += [
        "", "## 2. 基线消融", "",
        "| 口径 | 假设召回 | 矛盾 R | 陈旧 hit@5 | 陈旧 avoid 泄漏 |", "|---|---|---|---|---|",
        "| 无记忆 | 0% | 0% | 0% | 0% |",
        (f"| 朴素 top-k（纯词面） | {b['naive_topk']['hypothesis_recall']:.0%} "
        f"| {b['naive_topk']['contradiction']['recall']:.0%} "
        f"| {_avg(b['naive_topk']['staleness'], 'hit_at_k'):.0%} "
        f"| {_avg(b['naive_topk']['staleness'], 'avoid_leak'):.0%} |"),
        (f"| 全历史注入 | {b['full_history']['hypothesis_recall']:.0%} | 不适用 "
        f"| 100%（口径必然） | {_avg(b['full_history']['staleness'], 'avoid_leak'):.0%} |"),
        "", f"**结论：{r['conclusion']['verdict']}**", "",
    ]
    if r["fade_grid"]:
        lines += ["## 3. FadeMem 参数消融（β_LML × μ，θ 固定 0.7/0.3，SML=β+0.4）", "",
                  "| β_LML | μ | 陈旧 hit@5 | avoid 泄漏 | top5 平均强度 |",
                  "|---|---|---|---|---|"]
        for g in r["fade_grid"]:
            lines.append(f"| {g['beta_lml']} | {g['mu']} | "
                         f"{g['staleness_hit_at_k']:.0%} | {g['avoid_leak']:.0%} |"
                         f" {g.get('mean_top5_strength', 0):.3f} |")
        lines.append("")
    j = r["judge_consistency"]
    lines += ["## 4. judge 一致性", "",
              (f"- 一致 {j['consistent']}/{j['total']}（{j['ratio']:.0%}）" if j.get("evaluated")
               else f"- {j.get('note', '未评测')}"), ""]
    if r.get("brief_check_turn30"):
        lines += ["## 5. §七.1 第 30 会话开场简报自检", "",
                  (f"- {'PASS' if r['brief_check_turn30'].get('pass') else 'FAIL'} · "
                  f"{r['brief_check_turn30'].get('detail', '')}"), ""]
    return "\n".join(lines)


def _avg(rows: list[dict], key: str) -> float:
    return sum(x[key] for x in rows) / max(1, len(rows))


if __name__ == "__main__":
    sys.exit(main())
