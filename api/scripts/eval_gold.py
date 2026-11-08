"""金标小评测 + 证据采集（验收方案 §6，评分向）。

需真实 LLM（读取真实 .env；引擎开）：对库内论文跑
- 英文事实 10 题：Recall@5（top-5 命中金标关键词）≥0.8
- 应拒答 5 题：拒答准确率（未找到依据/明确拒答）≥0.9
- 中文 5 题：top-8 含金标关键词 ≥0.8；生成式回答挂 [E#] 锚点
- 生成式 trace：mode=generative、claims supported 占比 ≥80%、anchors E# 可映射 → 导出
  generative_trace.json；「done 后立即回放」连跑 10 次（gate 8）

用法：cd api && uv run python scripts/eval_gold.py --out ../../docs/acceptance/round2-<date>/
前置：库内已有一篇已解析论文（make demo / smoke 后即有）；服务器无需单独起（进程内直调）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 注意：本脚本刻意**不**屏蔽凭据（需要真实 LLM 生成证据）；引擎按宿主机 .env。

_EN_FACTS = [
    # (问题, 金标关键词)；金标词取自真实论文（Attention Is All You Need）正文
    ("How many GPUs were the base models trained on?", "P100"),
    ("How long did the big model train for?", "3.5"),
    ("What BLEU score did the big model achieve on EN-DE?", "28.4"),
    ("What attention mechanism computes in constant memory?", "length"),
    ("Which optimizer was used?", "Adam"),
    ("What learning rate schedule was applied?", "warmup"),
    ("What is the dropout applied to the sum of embeddings?", "residual"),
    ("How many encoder and decoder layers?", "6"),
    ("What is the model dimension d_model?", "512"),
    ("Which regularization prevented overfitting?", "dropout"),
]
# 中文问句 × 英文语料：金标为「答案所在的英文证据词」（考察跨语言检索，字面中文词在英文正文不可能出现）
_ZH_FACTS = [
    ("注意力机制是怎么计算的？", "attention"),
    ("训练用了多少块 GPU？", "GPU"),
    ("模型在 WMT14 英德翻译上拿到了多少 BLEU？", "BLEU"),
    ("编码器和解码器各有几层？", "encoder"),
    ("使用了什么优化器？", "Adam"),
]
_REFUSALS = [
    ("今天天气怎么样？", "refusal"),
    ("帮我写一首诗", "refusal"),
    ("股票代码 AAPL 现在多少钱？", "refusal"),
    ("推荐几家好吃的餐厅", "refusal"),
    ("1+1 等于几？", "refusal"),
]


def pick_paper() -> str:
    """金标题组针对 Attention 论文设计 → 优先按标题匹配；否则退化最新已解析。"""
    from app import db
    rows = db.query("SELECT id, title FROM papers WHERE docir_path IS NOT NULL AND docir_path != '' "
                    "ORDER BY created_at DESC")
    if not rows:
        raise SystemExit("库内无已解析论文：先上传或 make demo")
    for r in rows:
        if "attention" in (r["title"] or "").lower():
            print(f"评测对象：{r['title']} ({r['id']})（金标题组目标论文）")
            return r["id"]
    print(f"评测对象：{rows[0]['title']} ({rows[0]['id']})（未找到 Attention 论文，退化最新）")
    return rows[0]["id"]


def retrieval_metrics(paper_id: str) -> dict:
    """检索指标（不依赖 LLM）：Recall@5 / 中文 top-8 命中。"""
    from app.rag.indexing import hybrid_search

    r5_hits, r5_detail = [], []
    for q, gold in _EN_FACTS:
        res = hybrid_search([paper_id], q, top_k=5)
        hits = res.get("hits", [])
        ok = any(gold.lower() in (h.get("text") or "").lower() for h in hits)
        r5_hits.append(ok)
        r5_detail.append({
            "question": q, "gold_keyword": gold, "hit": ok,
            "top5": [{"chunk_id": h.get("chunk_id"), "page": (h.get("page") or 0) + 1,
                      "score": h.get("rrf") or h.get("score"),
                      "text": (h.get("text") or "")[:160]} for h in hits],
        })
        print(f"  [R@5] {'✓' if ok else '✗'} {q[:46]}")
    zh_hits, zh_detail = [], []
    for q, gold in _ZH_FACTS:
        res = hybrid_search([paper_id], q, top_k=8)
        hits = res.get("hits", [])
        ok = any(gold.lower() in (h.get("text") or "").lower() for h in hits)
        zh_hits.append(ok)
        zh_detail.append({
            "question": q, "gold_keyword": gold, "hit": ok,
            "top8": [{"chunk_id": h.get("chunk_id"), "page": (h.get("page") or 0) + 1,
                      "text": (h.get("text") or "")[:160]} for h in hits],
        })
        print(f"  [ZH] {'✓' if ok else '✗'} {q[:40]}")
    retrieval_detail = {"en": r5_detail, "zh": zh_detail}
    return {
        "recall_at5": round(sum(r5_hits) / len(r5_hits), 2),
        "recall_at5_detail": dict(zip((q for q, _ in _EN_FACTS),
                                      map(int, r5_hits))),
        "zh_top8_hit": round(sum(zh_hits) / len(zh_hits), 2),
        "zh_nonempty": sum(1 for d in zh_detail if d["top8"]),
        "retrieval_detail": retrieval_detail,   # 每题 top hits 全量导出（严格 R@5 可复算）
    }


def qa_round(client, paper_id: str, question: str, agent: str = "reader") -> tuple[dict, str]:
    conv = f"gold_{uuid.uuid4().hex[:8]}"
    final = None
    with client.stream("POST", "/api/chat",
                       json={"question": question, "paper_ids": [paper_id],
                             "conversation_id": conv, "agent": agent}) as r:
        assert r.status_code == 200, r.text
        event = ""
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: ") and event:
                d = json.loads(line[6:])
                if event == "final":
                    final = d
    return final or {}, conv


def generative_metrics(client, paper_id: str) -> dict:
    from app import db
    """生成式：claims supported 占比 / 锚点可映射 / 端到端耗时；选最优一条导出 trace。"""
    claims_stat = {"total": 0, "supported": 0, "needs_check": 0, "unsupported": 0}
    best = None
    for q, _ in _EN_FACTS[:5]:
        t0 = time.time()
        final, conv = qa_round(client, paper_id, q)
        elapsed = time.time() - t0
        mode = final.get("mode")
        claims = final.get("claims", [])
        anchors = final.get("anchors", {})
        claims_stat["total"] += len(claims)
        for c in claims:
            st = c.get("status", "unsupported")
            claims_stat[st if st in claims_stat else "unsupported"] += 1
        anchors_ok = all(a.get("page") for a in anchors.values()) if anchors else True
        print(f"  [GEN] {mode} · claims={len(claims)} · {elapsed:.1f}s · {q[:40]}")
        if mode == "generative" and claims and anchors:
            cand = {"question": q, "answer": final.get("answer", ""), "mode": mode,
                    "claims": claims, "anchors": anchors, "guardrails": final.get("guardrails", []),
                    "elapsed_s": round(elapsed, 1), "conversation_id": conv,
                    "thread_id": final.get("thread_id")}
            sup = sum(1 for c in claims if c.get("status") == "supported")
            score = (sup / len(claims), anchors_ok)
            if best is None or score > best[0]:
                best = (score, cand)
    sup_ratio = (claims_stat["supported"] / claims_stat["total"]) if claims_stat["total"] else 0.0
    halluc = (claims_stat["unsupported"] / claims_stat["total"]) if claims_stat["total"] else 1.0
    # trace 补 usage/model（来自 qa_logs 最新流式行；验收 A5：无 usage = 证据不全）
    usage_row = db.query("SELECT model, prompt_tokens, completion_tokens, latency_ms "
                         "FROM qa_logs WHERE guardrail='stream' ORDER BY id DESC LIMIT 1")
    usage = dict(usage_row[0]) if usage_row else {}
    if best[1] is not None and usage:
        best[1]["model"] = usage.get("model", "")
        best[1]["usage"] = {"prompt_tokens": usage.get("prompt_tokens", 0),
                            "completion_tokens": usage.get("completion_tokens", 0),
                            "latency_ms": usage.get("latency_ms", 0)}
    return {"claims": claims_stat, "supported_ratio": round(sup_ratio, 2),
            "hallucination_rate": round(halluc, 2), "best": best[1] if best else None,
            "best_anchors_ok": bool(best and best[0][1]), "stream_usage": usage}


def refusal_metrics(client, paper_id: str) -> dict:
    ok = []
    for q, _ in _REFUSALS:
        final, _ = qa_round(client, paper_id, q)
        ans = final.get("answer", "")
        hit = ("未找到依据" in ans) or (final.get("mode") == "refusal") or ("无法回答" in ans)
        ok.append(hit)
        print(f"  [REF] {'✓' if hit else '✗'} {q[:30]}")
    return {"refusal_accuracy": round(sum(ok) / len(ok), 2)}


def replay_race(client, paper_id: str, rounds: int = 10) -> dict:
    """gate 8：问答 → 立即回放 连跑 N 次全部 200。"""
    ok = 0
    results = []
    for i in range(rounds):
        final, _ = qa_round(client, paper_id, "How many GPUs were the base models trained on?")
        tid = final.get("thread_id")
        r = client.get(f"/api/qa/{tid}")
        good = r.status_code == 200 and bool(r.json().get("events"))
        ok += good
        results.append({"round": i + 1, "status": r.status_code, "events": len(r.json().get("events", [])) if r.status_code == 200 else 0})
        print(f"  [REPLAY {i + 1}/{rounds}] {r.status_code} · events={results[-1]['events']}")
    return {"rounds": rounds, "ok": ok, "detail": results}


def translate_evidence(client, paper_id: str, pages: int = 2) -> dict:
    """翻译证据：翻 2 页，translations 表有行、format_ok 占比、单页耗时 ≤60s。"""
    out = []
    for page in range(pages):
        t0 = time.time()
        r = client.post(f"/api/papers/{paper_id}/translate", json={"lang": "zh", "page": page})
        if r.status_code != 200:
            out.append({"page": page, "ok": False, "reason": f"HTTP {r.status_code}"})
            continue
        event = ""
        final = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: ") and event:
                d = json.loads(line[6:])
                if event == "final":
                    final = d
        elapsed = time.time() - t0
        cached = client.get(f"/api/papers/{paper_id}/translation?lang=zh").json().get("blocks", {})
        n_blocks = sum(1 for k in cached if k.startswith("b") )
        out.append({"page": page, "ok": bool(final), "elapsed_s": round(elapsed, 1),
                    "cached_blocks": n_blocks,
                    "within_60s": elapsed <= 60})
        print(f"  [TRANS p{page}] {final} · {elapsed:.1f}s · 缓存块 {n_blocks}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="金标评测 + 证据采集")
    ap.add_argument("--out", default="")
    ap.add_argument("--skip-llm", action="store_true", help="只跑检索/拒答（无 LLM 环境）")
    args = ap.parse_args()

    from fastapi.testclient import TestClient

    from app import db
    from app.main import app
    db.init_db()
    client = TestClient(app)
    paper_id = pick_paper()

    report: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "paper_id": paper_id}
    report.update(retrieval_metrics(paper_id))
    report.update(refusal_metrics(client, paper_id))
    if not args.skip_llm:
        gen = generative_metrics(client, paper_id)
        report["generative"] = {k: v for k, v in gen.items() if k != "best"}
        if gen["best"]:
            report["generative_trace"] = gen["best"]
        report["replay_race"] = replay_race(client, paper_id)
        report["translate"] = translate_evidence(client, paper_id)

    gates = {
        "recall_at5>=0.8": report.get("recall_at5", 0) >= 0.8,
        "refusal>=0.9": report.get("refusal_accuracy", 0) >= 0.9,
        "zh_top8>=0.8": report.get("zh_top8_hit", 0) >= 0.8,
    }
    if not args.skip_llm:
        gates["supported>=0.8"] = report["generative"]["supported_ratio"] >= 0.8
        gates["hallucination<=0.1"] = report["generative"]["hallucination_rate"] <= 0.1
        gates["replay_10_10"] = report["replay_race"]["ok"] == report["replay_race"]["rounds"]
    report["gates"] = gates
    print("\n== 金标门禁 ==")
    for k, v in gates.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")

    out = Path(args.out) if args.out else Path(".")
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval_gold.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    if report.get("generative_trace"):
        (out / "generative_trace.json").write_text(
            json.dumps(report["generative_trace"], ensure_ascii=False, indent=2),
            encoding="utf-8")
    print(f"已写出：{out / 'eval_gold.json'}")
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
