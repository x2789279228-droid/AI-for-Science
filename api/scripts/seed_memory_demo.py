#!/usr/bin/env python
"""30 会话记忆仿真生成器（模块七 P2，清单 §三#19 / 验收 §七.1）。

四条故事线（前期埋假设 → 中期埋实验/结论 → 后期矛盾/取代）+ 噪声会话撑起干扰面：
  A 被推翻：conv2 假设 → conv8/12 支持实验 → conv24 复现实验推翻（refutes 边）
  B 被支持：conv5 假设 → conv14 支持实验（supports 边）
  C 无证据：conv7 假设 → 无后续（unverified，不冒充）
  D 被取代：conv9 假设 → conv20 修正假设（supersedes → worker 复核 superseded）

产出（--out 目录）：
  <data-dir>/scholar.db        独立演示库（已存在则拒绝，--force 覆盖）
  seed_gold.json               金标（与事件同步产出）+ meta.human_review（20% 抽检，
                               status=pending——半人工口径）+ meta.acceptance_brief_check
                               （§七.1 自检：第 30 会话开场简报是否指出被推翻假设）
  seed_review_sample.md        20% 人工抽检清单（人工核对栏留空，待人工签字）

诚实口径：模拟语料 + 脚本自产金标 + 20% 人工抽检（半人工）；离线环境 supports 边为
直写 upsert（模拟 LLM 建边产物），refutes 边走规则建边路，均如实标注于 meta。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DAY = 86400.0
_TZ8 = timezone(timedelta(hours=8))
_USER = "local"

# ---------- 故事线（纯函数，不依赖 app——可单测） ----------

_HYP_A = "我假设稀疏注意力能把长文本推理的显存占用降低 40%"
_EV_A_SUP1 = "实验结果表明稀疏注意力显存占用下降 38%，与假设一致"
_EV_A_SUP2 = "复现 5 组长文本实验均显示稀疏注意力能明显降低显存占用"
_EV_A_REF = "复现实验推翻了稀疏注意力降低 40% 显存的假设：实测仅 12%，且长序列下反而升高"
_HYP_B = "我假设混合检索能把召回率提升 15 个百分点"
_EV_B_SUP = "实验证实混合检索召回率提升 16 个百分点，支持该假设"
_HYP_C = "我假设课程学习能加速小模型收敛"
_HYP_D = "我假设批处理管道是数据接入的最优解"
_HYP_D2 = "我修正假设：流式管道取代批处理管道成为数据接入最优解"

_NOISE = [
    "整理了本月的文献阅读清单", "讨论了可视化配色的可读性方案", "记录了组会关于评测集的讨论要点",
    "浏览了写作 Copilot 的大纲建议", "标记了两篇待精读的综述", "整理了实验设备的预约记录",
    "归档了旧版本的实验脚本", "更新了术语表中的中英对照", "记录了审稿意见的处理进度",
    "讨论了下一阶段的时间安排",
]


def build_plan(sessions: int = 30, seed: int = 42) -> list[list[dict]]:
    """生成 plans[conv_idx] = [draft,...]。draft 键：kind/content/subject/day_frac/
    importance/role。role ∈ hyp | ev_sup | ev_ref | refine | noise。纯函数可复现。"""
    rng = random.Random(seed)
    assert sessions >= 26, "故事线需要至少 26 个会话"
    plans: list[list[dict]] = [[] for _ in range(sessions)]

    def add(conv: int, role: str, kind: str, content: str, *, subject: str = "",
            importance: int = 5, day_frac: float = 0.5):
        plans[conv - 1].append({"kind": kind, "content": content, "subject": subject,
                                "importance": importance, "day_frac": day_frac, "role": role})

    # A 被推翻（conv2 → 8/12 支持 → 24 推翻）
    add(2, "hyp", "hypothesis", _HYP_A, subject="稀疏注意力", importance=8, day_frac=0.3)
    add(8, "ev_sup", "experiment_result", _EV_A_SUP1, subject="稀疏注意力", importance=6)
    add(12, "ev_sup", "experiment_result", _EV_A_SUP2, subject="稀疏注意力", importance=6)
    add(24, "ev_ref", "experiment_result", _EV_A_REF, subject="稀疏注意力", importance=8)
    # B 被支持（conv5 → 14 支持）
    add(5, "hyp", "hypothesis", _HYP_B, subject="混合检索", importance=7, day_frac=0.4)
    add(14, "ev_sup", "experiment_result", _EV_B_SUP, subject="混合检索", importance=6)
    # C 无证据（conv7）
    add(7, "hyp", "hypothesis", _HYP_C, subject="课程学习", importance=6, day_frac=0.5)
    # D 被取代（conv9 → 20 修正）
    add(9, "hyp", "hypothesis", _HYP_D, subject="数据管道", importance=7, day_frac=0.4)
    add(20, "refine", "hypothesis", _HYP_D2, subject="数据管道", importance=8, day_frac=0.5)
    # 噪声：未占用故事的会话各 1-2 条
    story_convs = {2, 5, 7, 8, 9, 12, 14, 20, 24}
    for conv in range(1, sessions + 1):
        if conv in story_convs:
            continue
        n = 1 if conv % 3 else 2
        for i in range(n):
            text = _NOISE[(conv + i * 3) % len(_NOISE)]
            add(conv, "noise", "topic" if conv % 2 else "finding", text,
                importance=2 + (conv + i) % 3, day_frac=rng.random())
    return plans


def flatten(plan: list[list[dict]], sessions: int, start_ts: float) -> list[dict]:
    """draft → 可入库事件行（id 占位由入库侧生成；此处产出 ts/kind/content/...）。"""
    rows: list[dict] = []
    for conv_idx, drafts in enumerate(plan, 1):
        conv_id = f"seed_c{conv_idx:02d}"
        for d in drafts:
            ts = start_ts + (conv_idx - 1 + d["day_frac"]) * DAY
            rows.append({**d, "ts": ts, "conversation_id": conv_id, "turn": len(rows) % 5 + 1,
                         "thread_id": f"{conv_id}:{len(rows) % 5 + 1}"})
    rows.sort(key=lambda r: r["ts"])
    return rows


def review_sample(rows: list[dict], ratio: float = 0.2, seed: int = 42) -> list[dict]:
    """20% 人工抽检样本（按事件种伪随机，含全部故事线事件——故事线是验收关键）。"""
    rng = random.Random(seed + 1)
    ids = [f"r{i}" for i, _ in enumerate(rows)]
    story_idx = [i for i, r in enumerate(rows) if r["role"] != "noise"]
    noise_idx = [i for i in ids and range(len(rows)) if rows[i]["role"] == "noise"]
    picked = set(story_idx)
    n_noise = max(0, round(len(rows) * ratio) - len(story_idx))
    picked |= set(rng.sample(noise_idx, min(n_noise, len(noise_idx))))
    return [{"idx": i, **{k: rows[i][k] for k in ("kind", "content", "conversation_id")}}
            for i in sorted(picked)]


def build_gold(ids: dict[str, str], n_events: int, sessions: int) -> dict:
    """金标（与事件同步产出）。ids：role → memory_event_id 映射。"""
    return {
        "meta": {
            "sessions": sessions, "n_events": n_events,
            "generated_at": datetime.now(_TZ8).isoformat(),
            "honest": "模拟语料 + 脚本自产金标 + 20% 人工抽检（半人工，清单 §七.4）",
            "human_review": {"ratio": 0.2, "sampled": None, "status": "pending",
                             "note": "抽检清单见 seed_review_sample.md；人工核对后置 passed"},
            "edges": {"refutes": "规则建边路（推翻标记）",
                      "supports": "直写 upsert（模拟 LLM 建边产物，离线环境如实标注）"},
        },
        "hypotheses": [
            {"role": "A", "expect_id": ids["hyp_A"], "expect_verdict": "refuted",
             "content": _HYP_A},
            {"role": "B", "expect_id": ids["hyp_B"], "expect_verdict": "supported",
             "content": _HYP_B},
            {"role": "C", "expect_id": ids["hyp_C"], "expect_verdict": "unverified",
             "content": _HYP_C},
            {"role": "D", "expect_id": ids["hyp_D"], "expect_verdict": None,
             "expect_past": True, "content": _HYP_D},
            {"role": "D2", "expect_id": ids["hyp_D2"], "expect_verdict": "any",
             "expect_past": False, "content": _HYP_D2,
             "note": "只要求保持活跃（新方向无证据 → unverified 合法），不锁具体 verdict"},
        ],
        "contradiction_pairs": [
            {"hyp": ids["hyp_A"], "evidence": ids["ev_ref"], "expect_relation": "refutes"},
        ],
        "staleness": [
            {"query": "现在长文本显存优化该用什么方案？",
             "expect_ids": [ids["ev_ref"]], "avoid_ids": [ids["ev_sup_A1"], ids["ev_sup_A2"]]},
            {"query": "数据接入管道的当前结论是什么？",
             "expect_ids": [ids["hyp_D2"]], "avoid_ids": [ids["hyp_D"]]},
            {"query": "最近两周长文本显存优化的实验结论",
             "expect_ids": [ids["ev_ref"]], "avoid_ids": [ids["ev_sup_A1"], ids["ev_sup_A2"]],
             "note": "时间窗口径：timeparse 解析「最近两周」→ 窗外旧支持实验被滤除"},
        ],
        "brief_check_turn30": {
            "expect": "refuted", "expect_id": ids["hyp_A"],
            "note": "§七.1：第 30 会话开场简报为用户级研究简报（含全部活跃假设）",
        },
    }


def render_review_md(sample: list[dict]) -> str:
    lines = ["# 种子数据 20% 人工抽检清单（半人工金标）", "",
             "> 核对每条内容与其 kind/conversation 是否自洽；核对完成后将 gold meta 的",
             "> human_review.status 置为 passed 并签署。", ""]
    for s in sample:
        lines.append(f"- [ ] ({s['idx']}) [{s['kind']}] {s['content']}  （{s['conversation_id']}）")
    return "\n".join(lines) + "\n"


# ---------- 入库（app 依赖延迟到 env 设置之后） ----------

def _boot(data_dir: Path) -> None:
    import os
    os.environ["SCHOLAR_DATA_DIR"] = str(data_dir)
    for m in [k for k in list(sys.modules) if k.startswith("app")]:
        del sys.modules[m]                     # 防重复运行时 DATA_DIR 锁死


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data/seed_mem")
    ap.add_argument("--out", default="docs/acceptance/round9-20261121-r7")
    ap.add_argument("--sessions", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--force", action="store_true", help="覆盖已存在的演示库")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out)
    db_file = data_dir / "scholar.db"
    if db_file.exists() and not args.force:
        print(f"[seed] 拒绝覆盖已存在库：{db_file}（--force 覆盖）")
        return 2
    if db_file.exists():
        db_file.unlink()
    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    _boot(data_dir)
    from app import db
    from app.memory import briefing, graph, store, worker

    db.init_db()
    start_ts = (datetime(2026, 10, 20, 9, 0, tzinfo=_TZ8).timestamp())
    plan = build_plan(args.sessions, args.seed)
    rows = flatten(plan, args.sessions, start_ts)

    ids: dict[str, str] = {}
    for r in rows:
        ev = store.add_event(r["kind"], r["content"], user_id=_USER,
                             conversation_id=r["conversation_id"], turn=r["turn"],
                             thread_id=r["thread_id"], subject=r["subject"],
                             importance=r["importance"], ts=r["ts"], source="rule")
        # 角色 → 金标 id 映射（按会话与内容定位）
        if r["conversation_id"] == "seed_c02" and r["role"] == "hyp":
            ids["hyp_A"] = ev["id"]
        elif r["conversation_id"] == "seed_c05" and r["role"] == "hyp":
            ids["hyp_B"] = ev["id"]
        elif r["conversation_id"] == "seed_c07" and r["role"] == "hyp":
            ids["hyp_C"] = ev["id"]
        elif r["conversation_id"] == "seed_c09" and r["role"] == "hyp":
            ids["hyp_D"] = ev["id"]
        elif r["conversation_id"] == "seed_c20" and r["role"] == "refine":
            ids["hyp_D2"] = ev["id"]
        elif r["conversation_id"] == "seed_c08":
            ids.setdefault("ev_sup_A1", ev["id"])
        elif r["conversation_id"] == "seed_c12":
            ids.setdefault("ev_sup_A2", ev["id"])
        elif r["conversation_id"] == "seed_c24" and r["role"] == "ev_ref":
            ids["ev_ref"] = ev["id"]
        elif r["conversation_id"] == "seed_c14" and r["role"] == "ev_sup":
            ids["ev_sup_B"] = ev["id"]

    # 建边：A 矛盾（规则路 refutes）；A/B 支持（直写 upsert——模拟 LLM 建边产物，如实标注）
    n_edges = 0
    if "ev_ref" in ids:
        n_edges += len(graph.link_event(ids["ev_ref"], user_id=_USER))
    for sup_key, hyp_key in (("ev_sup_B", "hyp_B"),):
        if sup_key in ids and hyp_key in ids:
            store.upsert_edge(ids[sup_key], ids[hyp_key], "supports",
                              evidence={"engine": "seed-direct", "note": "模拟 LLM 建边产物"})
            n_edges += 1
    # 支持实验也支持 A？——规则口径：A 的支持实验不建 supports 边（规则路无 supports 标记），
    # A 的 verdict 由 refutes 边决定（refuted）——支持实验仅作为陈旧判断的 avoid 目标。

    worker.run_once(user_id=_USER)             # FSM 复核（D→superseded）+ 强度 + 预热

    gold = build_gold(ids, len(rows), args.sessions)
    sample = review_sample(rows, ratio=0.2, seed=args.seed)
    gold["meta"]["human_review"]["sampled"] = len(sample)

    # §七.1 自检：第 30 会话开场简报（用户级研究简报）须指出被推翻假设。
    # 判定后假设已 FSM 推进至终态 → 出现在 past_hypotheses（status=refuted）即为
    # "简报指出被推翻"；活跃项（items）的 verdict=refuted 同样算指出。
    brief = briefing.build_brief(user_id=_USER, use_judge=False)
    found_active = next((i["verdict"] for i in brief["items"]
                         if i["hypothesis"]["id"] == ids["hyp_A"]), None)
    found_past = next((p["status"] for p in brief.get("past_hypotheses", [])
                       if p["id"] == ids["hyp_A"]), None)
    expect = gold["brief_check_turn30"]["expect"]
    outcome = found_active or found_past
    check_pass = outcome == expect
    gold["meta"]["acceptance_brief_check"] = {
        "pass": check_pass,
        "detail": f"假设 A 判定结果={outcome or 'missing'}（期望 {expect}）；"
                  f"位置={'items' if found_active else ('past_hypotheses' if found_past else '无')}"
                  f"；engine={brief['engine']}",
    }

    (out_dir / "seed_gold.json").write_text(
        json.dumps(gold, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "seed_review_sample.md").write_text(render_review_md(sample), encoding="utf-8")
    print(f"[seed] 事件 {len(rows)} 条 / 会话 {args.sessions} / 边 {n_edges} 条")
    print(f"[seed] §七.1 简报自检：{'PASS' if check_pass else 'FAIL'} — "
          f"{gold['meta']['acceptance_brief_check']['detail']}")
    print(f"[seed] 金标 → {out_dir / 'seed_gold.json'}；抽检清单（{len(sample)} 条）→ "
          f"{out_dir / 'seed_review_sample.md'}")
    return 0 if check_pass else 1


if __name__ == "__main__":
    sys.exit(main())
