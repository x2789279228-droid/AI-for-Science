#!/usr/bin/env python
"""底层模型能力基准：记忆问答合成任务（模块七 r10 遗留痛点的模型归因实验）。

背景：r9/r10 A/B 实测 doubao-seed-2.1-lite 在「跨条目盘点综合」类记忆问答上
合成不足（q3 漏报、q2 方差）。本基准用**生产同款 memory_refine 提示词 + 真实
演示库时间线**，对候选模型做同题对比：
  doubao-seed-2.1-lite（现状基线）/ doubao-seed-2.1-pro / deepseek-v4.1-flash

评分（客观锚点，时间线内事实）：
  q1 现状：锚点 [推翻, 12]（须指出被推翻+实测12%，而非复述旧目标40%）
  q2 决策：锚点 [流式]（须转向流式方案）
  q3 盘点：锚点 [课程学习, 流式]（两条未验证假设都须列出——lite 已知漏报场景）
  q4 历史：锚点 [16, 支持]（验证结果 16 个百分点 + 被支持）

用法：python api/scripts/benchmark_memory_models.py --reps 3
产出：docs/acceptance/round9-20261121-r10/model_benchmark.json + 控制台表格
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

_TIMELINE = """[M1] 我假设稀疏注意力能把长文本推理的显存占用降低 40%（当前状态：已推翻）
[M2] 复现实验推翻了稀疏注意力降低 40% 显存的假设：实测仅 12%，且长序列下反而升高
[M3] 我假设混合检索能把召回率提升 15 个百分点（当前状态：已支持）
[M4] 实验证实混合检索召回率提升 16 个百分点，支持该假设
[M5] 我假设课程学习能加速小模型收敛（当前状态：提出）
[M6] 我修正假设：流式管道取代批处理管道成为数据接入最优解（当前状态：提出）
[M7] 我假设批处理管道是数据接入的最优解（当前状态：已被取代）"""

_SYSTEM = (
    "你是用户的科研记忆助手。用户的提问是关于**他本人的历史研究**（过去的假设、实验、"
    "决定、偏好及其现状），你必须**只依据**下方给出的记忆时间线回答，并遵守：\n"
    "1. 每个事实性论断后紧跟 [M编号] 引用（编号取自时间线标签，如 [M1]）。\n"
    "2. 注意记忆的状态：status=refuted（已推翻）/superseded（已被取代）的记忆是"
    "**历史结论**，陈述时必须说明其已不成立，禁止当作当前有效结论复述。\n"
    "3. 直接回答问题本身；不要谈论「某论文是否提及/验证」——这是记忆问答，不是"
    "文献问答；论文证据不在本时间线内。\n"
    "4. 时间线没有的信息如实说明「记忆中没有相关记录」，禁止编造。\n"
    "5. 回答用中文，简洁分点，≤250 字。")

_QUESTIONS = [
    {"qid": "q1_status", "question": "我之前关于显存优化的假设，现在的结论是什么？",
     "anchors": ["推翻", "12"]},
    {"qid": "q2_decision", "question": "数据接入管道现在应该采用什么方案？",
     "anchors": ["流式"]},
    {"qid": "q3_open", "question": "我还有哪些假设是没有被验证过的？",
     "anchors": ["课程学习", "流式"]},
    {"qid": "q4_history", "question": "当初混合检索提升召回率的假设，后来验证得怎么样？",
     "anchors": ["16", "支持"]},
]

MODELS = ["doubao-seed-2.1-lite", "doubao-seed-2.1-pro", "deepseek-v4.1-flash"]


def _chat(base: str, key: str, model: str, question: str, max_tokens: int = 700):
    """生产同款 memory_refine 调用。doubao 系关闭深度思考（与产线一致）。"""
    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content":
                 f"记忆时间线（[M#] 编号，hypothesis 附当前状态）：\n{_TIMELINE}\n\n"
                 f"用户问题：{question}"}]
    payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if model.startswith("doubao"):
        payload["thinking"] = {"type": "disabled"}   # 产线口径：关闭深度思考
    t0 = time.time()
    r = httpx.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"},
                   json=payload, timeout=120)
    elapsed = round(time.time() - t0, 1)
    body = r.json()
    msg = (body.get("choices") or [{}])[0].get("message", {})
    content = (msg.get("content") or "").strip()
    err = ""
    if isinstance(body.get("error"), dict):
        err = body["error"].get("message", "")[:100]
    elif not content:
        err = f"空 content（finish={body.get('choices', [{}])[0].get('finish_reason')}）"
    return content, err, elapsed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default="../docs/acceptance/round9-20261121-r10/model_benchmark.json")
    args = ap.parse_args()

    env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    base = env["LLM_BASE_URL"].rstrip("/")
    key = env["LLM_API_KEY"]

    results: list[dict] = []
    for model in MODELS:
        for q in _QUESTIONS:
            reps = []
            for rep in range(args.reps):
                content, err, elapsed = _chat(base, key, model, q["question"])
                if err and not content:
                    reps.append({"anchor_rate": None, "cited": False, "err": err,
                                 "elapsed": elapsed})
                    continue
                hits = [a for a in q["anchors"] if a in content]
                cited = bool(re.search(r"\[M\d+\]", content))
                reps.append({"anchor_rate": round(len(hits) / len(q["anchors"]), 3),
                             "hits": hits, "cited": cited, "elapsed": elapsed,
                             "answer_head": content[:120]})
                time.sleep(0.4)
            valid = [r for r in reps if r.get("anchor_rate") is not None]
            avg = round(sum(r["anchor_rate"] for r in valid) / len(valid), 3) if valid else None
            results.append({"model": model, "qid": q["qid"], "reps": reps,
                            "anchor_rate_avg": avg,
                            "all_reps_full_hit": bool(valid) and
                            all(r["anchor_rate"] == 1.0 for r in valid),
                            "any_miss": bool(valid) and
                            any(r["anchor_rate"] < 1.0 for r in valid),
                            "n_err": sum(1 for r in reps if r.get("err"))})
            print(f"[bench] {model} / {q['qid']}: anchor_avg={avg} "
                  f"全中={results[-1]['all_reps_full_hit']} errs={results[-1]['n_err']}")

    # 汇总
    summary = {}
    for model in MODELS:
        rows = [r for r in results if r["model"] == model]
        rates = [r["anchor_rate_avg"] for r in rows if r["anchor_rate_avg"] is not None]
        lat = [rr["elapsed"] for r in rows for rr in r["reps"]
               if rr.get("elapsed") is not None]
        summary[model] = {
            "anchor_avg_overall": round(sum(rates) / max(1, len(rates)), 3)
            if rates else None,
            "per_question": {r["qid"]: r["anchor_rate_avg"] for r in rows},
            "full_hit_questions": sum(1 for r in rows if r["all_reps_full_hit"]),
            "questions_total": len(rows),
            "total_errs": sum(r["n_err"] for r in rows),
            "avg_latency_s": round(sum(lat) / max(1, len(lat)), 1) if lat else None,
        }
    out = {"meta": {"reps": args.reps, "questions": len(_QUESTIONS),
                    "task": "记忆问答合成（memory_refine 生产提示词 + 真实时间线）",
                    "generated_at": time.strftime("%Y-%m-%d %H:%M %z")},
           "summary": summary, "results": results}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 汇总（锚点命中率，3 次重复均值）===")
    print(f"{'模型':<26}{'q1':>8}{'q2':>8}{'q3':>8}{'q4':>8}{'均分':>8}")
    for model in MODELS:
        s = summary[model]
        pq = s["per_question"]
        print(f"{model:<26}"
              f"{pq.get('q1_status', 0):>8.0%}{pq.get('q2_decision', 0):>8.0%}"
              f"{pq.get('q3_open', 0):>8.0%}{pq.get('q4_history', 0):>8.0%}"
              f"{s['anchor_avg_overall']:>8.0%}")
    print(f"\n结果 → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
