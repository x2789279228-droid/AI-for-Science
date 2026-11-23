#!/usr/bin/env python
"""检索权重网格快评（模块七 r10-E1/E2）：无 LLM、纯离线、秒级扫参。

对 seed 演示库的四条探针（现状/决策/盘点/历史）在参数网格上离线评估注入时间线的
期望覆盖 / 陈旧泄漏 / 盘点完整度——用于回答「三信号等权是否合理、每类问题的最优
权重是什么」（r9 A/B judge 1.8 短板的归因实验）。

金标（按内容子串定位事件，与 seed_memory_demo 故事线对应）：
  q1 现状：期望=矛盾实验(12%)；避免=两条旧支持实验（词面相关但已被推翻假设的证据）
  q2 决策：期望=流式管道修正假设；避免=批处理旧假设
  q3 盘点：期望=全部 5 条假设都在时间线中（完整度）；重点=未验证的课程学习假设
  q4 历史：期望=混合检索支持实验(16%)+假设本身

用法：
  SCHOLAR_DATA_DIR=data/seed_mem python api/scripts/eval_weight_sweep.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os_set = __import__("os")
if "SCHOLAR_DATA_DIR" not in os_set.environ:
    os_set.environ["SCHOLAR_DATA_DIR"] = str(Path(__file__).resolve().parents[1] / "data" / "seed_mem")

from app.config import settings  # noqa: E402
from app.memory import retrieve, store  # noqa: E402

END_TS = time.time()
TOPK_CANDIDATES = [3, 5]
WEIGHT_GRID = [
    (1.0, 1.0, 1.0),    # 等权（r9 现状基线）
    (0.3, 1.0, 1.2),    # recency 抑制（历史/盘点向）
    (1.5, 1.0, 1.2),    # recency+relevance 强化（现状向）
    (0.0, 1.0, 0.5),    # 无 recency（纯盘点）
    (0.3, 0.8, 1.2),    # recency 抑制 + importance 弱化
    (1.2, 1.0, 0.8),    # recency 强、relevance 弱
    (0.5, 1.5, 1.0),    # importance 主导
]
STALE_GRID = [0.1, 0.3, 0.6]

# 探针金标（内容子串 → 事件定位，运行时解析为 id）
PROBES = [
    {"qid": "q1_status", "intent": "current",
     "question": "我之前关于显存优化的假设，现在的结论是什么？",
     "expect": ["实测仅 12%"], "avoid": ["下降 38%", "复现 5 组"]},
    {"qid": "q2_decision", "intent": "current",
     "question": "数据接入管道现在应该采用什么方案？",
     "expect": ["流式管道取代批处理"], "avoid": ["批处理管道是数据接入的最优解"]},
    {"qid": "q3_open", "intent": "inventory",
     "question": "我还有哪些假设是没有被验证过的？",
     "expect": ["课程学习"], "avoid": [],
     "inventory": True},
    {"qid": "q4_history", "intent": "history",
     "question": "当初混合检索提升召回率的假设，后来验证得怎么样？",
     "expect": ["16 个百分点", "混合检索能把召回率提升"], "avoid": []},
]


def _load_gold_ids() -> dict[str, str]:
    """内容子串 → 事件 id（seed 故事线事件定位）。"""
    out: dict[str, str] = {}
    for e in store.list_events(limit=500):
        if e["user_id"] != "local":
            continue
        for probe in PROBES:
            for key, subs in (("expect", probe["expect"]), ("avoid", probe["avoid"])):
                for sub in subs:
                    if sub in e["content"]:
                        out[f"{probe['qid']}:{key}:{sub}"] = e["id"]
    return out


def _timeline_for(question: str, *, top_k: int, max_events: int = 8) -> list[str]:
    """复现注入选择（只读）：检索 → rel>0 种子 → 时间线路径的事件 id 列表。"""
    res = retrieve.retrieve_memories(question, user_id="local", top_k=top_k,
                                     now_ts=END_TS, touch=False)
    from app.memory import graph as _graph
    seed_ids = [i["id"] for i in res["items"] if i["scores"]["hops"] == 0
                and i["scores"]["relevance_raw"] > 0]
    if not seed_ids:
        return []
    path = _graph.timeline_path(seed_ids, max_events=max_events)
    return [e["id"] for e in path["events"]]


def main() -> int:
    from app.memory import graph as _graph
    from app.db import tx

    # 基线重置（同 eval_ab_longterm 口径）：清除评测期抽取写入的 ab_* 会话事件——
    # 其 recency 与词面双重优势会污染扫参结果（r10 实测：q4 覆盖全配置为 0 的根因）
    with tx() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM memory_events WHERE conversation_id LIKE 'ab|_%' ESCAPE '|'")]
        for i in ids:
            c.execute("DELETE FROM memory_edges WHERE src=? OR dst=?", (i, i))
        c.execute("DELETE FROM memory_events WHERE conversation_id LIKE 'ab|_%' ESCAPE '|'")
    print(f"[sweep] 基线重置：清除评测期残留 {len(ids)} 条")

    gold_subs = _load_gold_ids()
    # 探针期望/避免 → id
    probes_resolved = []
    for p in PROBES:
        expect = [gold_subs[f"{p['qid']}:expect:{sub}"] for sub in p["expect"]
                  if f"{p['qid']}:expect:{sub}" in gold_subs]
        avoid = [gold_subs[f"{p['qid']}:avoid:{sub}"] for sub in p["avoid"]
                 if f"{p['qid']}:avoid:{sub}" in gold_subs]
        probes_resolved.append({**p, "expect_ids": expect, "avoid_ids": avoid})
    all_hyps = {e["id"] for e in store.list_events(kind="hypothesis", limit=100)
                if e["user_id"] == "local"}
    print(f"[sweep] 金标解析完成：{len(gold_subs)} 个事件引用；假设池 {len(all_hyps)} 条")

    rows = []
    best_by_probe: dict[str, tuple[float, dict]] = {p["qid"]: (-1.0, {}) for p in PROBES}
    for w_r, w_i, w_rel in WEIGHT_GRID:
        for stale in STALE_GRID:
            for top_k in TOPK_CANDIDATES:
                settings.ret_w_recency = w_r
                settings.ret_w_importance = w_i
                settings.ret_w_relevance = w_rel
                settings.ret_stale_penalty = stale
                cfg = {"w": [w_r, w_i, w_rel], "stale": stale, "top_k": top_k}
                per_probe = []
                for p in probes_resolved:
                    tl = _timeline_for(p["question"], top_k=top_k)
                    expect_cov = (len(set(p["expect_ids"]) & set(tl))
                                  / len(p["expect_ids"])) if p["expect_ids"] else 1.0
                    avoid_leak = (len(set(p["avoid_ids"]) & set(tl))
                                  / len(p["avoid_ids"])) if p["avoid_ids"] else 0.0
                    inv = 1.0
                    if p.get("inventory"):
                        inv = len(set(tl) & all_hyps) / max(1, len(all_hyps))
                    score = round(expect_cov * 0.5 + (1 - avoid_leak) * 0.2 + inv * 0.3, 4)
                    per_probe.append({"qid": p["qid"], "expect_cov": round(expect_cov, 3),
                                      "avoid_leak": round(avoid_leak, 3),
                                      "inventory_completeness": round(inv, 3),
                                      "score": score})
                    if score > best_by_probe[p["qid"]][0]:
                        best_by_probe[p["qid"]] = (score, {**cfg, "detail": per_probe[-1]})
                    _ = per_probe
                avg = round(sum(x["score"] for x in per_probe) / len(per_probe), 4)
                rows.append({**cfg, "avg_score": avg, "per_probe": per_probe})
                if avg > max((r["avg_score"] for r in rows[:-1]), default=0):
                    print(f"[sweep] 新最优 avg={avg} w={cfg['w']} stale={stale} top_k={top_k}")

    # 恢复默认
    settings.ret_w_recency = settings.ret_w_importance = settings.ret_w_relevance = 1.0
    settings.ret_stale_penalty = 0.3

    # 按意图汇总最优（E2）
    by_intent: dict[str, dict] = {}
    for p, (score, cfg) in best_by_probe.items():
        intent = next(x["intent"] for x in PROBES if x["qid"] == p)
        by_intent.setdefault(intent, {"best_score": -1})
        if score > by_intent[intent]["best_score"]:
            by_intent[intent] = {"best_score": score, "probe": p, **cfg}

    out = {"grid_rows": rows, "best_by_probe": {k: v for k, v in best_by_probe.items()},
           "best_by_intent": by_intent}
    out_path = Path(__file__).resolve().parents[2] / "docs" / "acceptance" / \
        "round9-20261121-r10" / "weight_sweep.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[sweep] 共 {len(rows)} 组配置 → {out_path}")
    print("[sweep] 按意图最优（E2）：")
    for intent, d in by_intent.items():
        print(f"  {intent}: score={d['best_score']} w={d.get('w')} "
              f"stale={d.get('stale')} top_k={d.get('top_k')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
