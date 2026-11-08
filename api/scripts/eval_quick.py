"""轻量 smoke 评测（答辩自证数字用，非完整金标集）。

跑一轮内置问题集（含应拒答题），统计：
- 引用有效率：答案含 [E#] 锚点且无 invalid_citation guardrail 的比例
- 锚点有效率：全部 [E#] 均通过 L1 后验（无 invalid_citation:* / no_citation_found）
- 拒答正确率：应拒答题正确输出「未找到依据/拒答」的比例
- 端到端延迟（首 final 事件耗时）

用法：
  uv run python scripts/eval_quick.py                 # 全部库内论文
  uv run python scripts/eval_quick.py --paper p_xxx   # 指定论文
"""
from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # api/

from app import db
from app.agents import runner

CIT = re.compile(r"\[E(\d+)\]")

QA_SET = [
    # 细粒度事实（期望带锚点作答）
    ("How many GPUs was the model trained on?", "answer"),
    ("What BLEU score does the base model achieve on WMT 2014 English-to-German?", "answer"),
    ("How long was the big model trained?", "answer"),
    ("Which optimizer and learning rate schedule does the model use?", "answer"),
    ("What is the position-wise feed-forward layer dimension?", "answer"),
    # 应拒答（期望明确「未找到依据」/ 范围拒答）
    ("How do you cook braised pork belly at home?", "refuse"),
    ("What is the Schrödinger equation in quantum mechanics?", "refuse"),
    ("哪支球队获得了 2022 年世界杯冠军？", "refuse"),
    ("明天北京的天气怎么样？", "refuse"),
    ("请推荐三本科幻小说。", "refuse"),
]


def run_one(question: str, paper_ids: list[str]) -> dict:
    q: queue.Queue = queue.Queue()
    t0 = time.time()
    runner.run_stream(question, paper_ids, q)
    final = None
    while True:
        kind, data = q.get(timeout=120)
        if kind == "final":
            final = data
            break
        if kind == "done":
            break
    elapsed = round(time.time() - t0, 1)
    final = final or {}
    answer = final.get("answer", "")
    guards = final.get("guardrails", [])
    mode = final.get("mode", "none")
    n_claims = len(final.get("claims", []))
    if final.get("mode") in ("refusal", "none") or "未找到依据" in answer:
        behave = "refuse"
    else:
        behave = "answer"
    anchor_ok = bool(CIT.search(answer)) and not any(
        g.startswith("invalid_citation") or g == "no_citation_found" for g in guards)
    return {"question": question, "expect": "", "behave": behave, "mode": mode,
            "answer": answer, "guards": guards, "n_claims": n_claims,
            "n_anchors": len(CIT.findall(answer)), "anchor_ok": anchor_ok,
            "elapsed": elapsed}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="append", default=[], help="限定 paper_id（可多次）")
    args = ap.parse_args()

    db.init_db()
    paper_ids = args.paper or [r["id"] for r in db.query("SELECT id FROM papers ORDER BY created_at")]
    if not paper_ids:
        print("文献库为空：先上传 PDF（POST /api/papers）。")
        return
    titles = {r["id"]: r["title"] for r in db.query("SELECT id,title FROM papers")}
    print(f"评测范围：{len(paper_ids)} 篇 → {', '.join(titles.get(p, p)[:20] for p in paper_ids)}\n")

    rows = []
    for question, expect in QA_SET:
        r = run_one(question, paper_ids)
        r["expect"] = expect
        rows.append(r)
        flag = "✓" if r["behave"] == expect else "✗"
        print(f"[{flag}] {question[:44]:46} → {r['behave']:7} mode={r['mode']:10} "
              f"anchors={r['n_anchors']:2} claims={r['n_claims']:2} {r['elapsed']:5}s "
              + (f"guards={r['guards']}" if r["guards"] else ""))

    answers = [r for r in rows if r["expect"] == "answer"]
    refuses = [r for r in rows if r["expect"] == "refuse"]
    n_ans_ok = sum(1 for r in answers if r["behave"] == "answer")
    n_anchor_ok = sum(1 for r in answers if r["anchor_ok"])
    n_refuse_ok = sum(1 for r in refuses if r["behave"] == "refuse")
    n_lat = sum(r["elapsed"] for r in rows) / max(len(rows), 1)
    print("\n===== smoke 评测汇总 =====")
    print(f"作答正确（应答 {n_ans_ok}/{len(answers)}）")
    print(f"引用锚点有效率（含 [E#] 且全部通过 L1 后验）：{n_anchor_ok}/{len(answers)}"
          f" = {n_anchor_ok / max(len(answers), 1):.0%}")
    print(f"拒答正确率（应拒答）：{n_refuse_ok}/{len(refuses)}"
          f" = {n_refuse_ok / max(len(refuses), 1):.0%}")
    print(f"平均端到端延迟：{n_lat:.1f}s")
    out = Path(__file__).parent / "eval_quick_last.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"明细已写入 {out}")


if __name__ == "__main__":
    main()
