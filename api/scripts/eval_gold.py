"""金标评测 + 证据采集（验收方案 §6 + 细粒度/抗幻觉 P0 扩展）。

需真实 LLM（读取真实 .env；引擎开）：对库内论文跑
- 英文事实 10 题：Recall@5 宽口径（gate）+ 严格口径（token 边界）+ MRR@5（报告字段）
- 官方考点 8 问（方案 §2.2）：作者机构/β1β2/GPU/BLEU/论证链/[11] 引用 → rubric8 ≥0.75
  （#7 BERT 对比与 #8 GPT 由 compare_metrics / 语料题承接，需 fetch_corpus 语料）
- 应拒答 5 题 ≥0.9；中文 5 题 top-8 ≥0.8
- 生成式：supported ≥80%、hallucination ≤10%、anchor_precision（claim 原句块级
  bbox 存在且 quote 归一包含）≥0.7、回放 10/10
- 多篇对比端到端：compare_metrics（表格/格级 E#/覆盖/对称断言）→ compare_ok

用法：cd api && uv run python scripts/eval_gold.py --out ../../docs/acceptance/round2-<date>/
前置：库内有已解析论文（对比评测需先跑 scripts/fetch_corpus.py 收录 BERT）；进程内直调。
"""
from __future__ import annotations

import argparse
import json
import re
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
    """检索指标（不依赖 LLM）：宽口径 Recall@5（gate）+ 严格口径（token 边界）+ MRR@5。"""
    import re as _re

    from app.rag.indexing import hybrid_search

    def _strict_hit(gold: str, text: str) -> bool:
        return _re.search(rf"(?<!\w){_re.escape(gold)}(?!\w)", text or "", _re.IGNORECASE) is not None

    r5_hits, r5_strict, r5_rr, r5_detail = [], [], [], []
    for q, gold in _EN_FACTS:
        res = hybrid_search([paper_id], q, top_k=5)
        hits = res.get("hits", [])
        ok = any(gold.lower() in (h.get("text") or "").lower() for h in hits)
        strict = any(_strict_hit(gold, h.get("text") or "") for h in hits)
        rank = next((i + 1 for i, h in enumerate(hits)
                     if gold.lower() in (h.get("text") or "").lower()), 0)
        r5_hits.append(ok)
        r5_strict.append(strict)
        r5_rr.append(1.0 / rank if rank else 0.0)
        r5_detail.append({
            "question": q, "gold_keyword": gold, "hit": ok, "strict_hit": strict,
            "first_hit_rank": rank,
            "top5": [{"chunk_id": h.get("chunk_id"), "page": (h.get("page") or 0) + 1,
                      "score": h.get("rrf") or h.get("score"),
                      "text": (h.get("text") or "")[:160]} for h in hits],
        })
        print(f"  [R@5] {'✓' if ok else '✗'}{'+s' if strict else ''} r{rank} {q[:42]}")
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
        "strict_recall_at5": round(sum(r5_strict) / len(r5_strict), 2),
        "mrr_at5": round(sum(r5_rr) / len(r5_rr), 2),
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
    """生成式：claims supported 占比 / 锚点一致性（原句块级）/ 端到端耗时；选最优导出 trace。"""
    from app.rag.verify import _norm as _norm_txt
    from app.rag.version_diff import load_docir_for

    claims_stat = {"total": 0, "supported": 0, "needs_check": 0, "unsupported": 0}
    # 锚点一致性（P0 修订）：claim 首 support 的原句块 bbox 存在且 quote 归一包含于该块文本
    block_map: dict[str, str] = {}
    docir = load_docir_for(paper_id) or {}
    for b in docir.get("blocks", []):
        block_map[b.get("id", "")] = _norm_txt(b.get("text", ""))
    prec_n, prec_ok = 0, 0
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
        # anchor_precision（P0 口径）：在 **L2 verified** 的 claim 上度量定位精度——
        # 首 support 原句块存在且 quote 对该块覆盖率 ≥0.85（与 L2 同阈值；estimated
        # 块若覆盖达标同样计入）；转述/拼凑引文（unverified）不定位也不计入分母
        # （其诚实信号是 quote_status 徽章，不是块锚点）
        from app.rag.verify import quote_coverage as _qcov
        for c in claims:
            sups = c.get("support", [])
            if not sups or c.get("quote_status") != "verified":
                continue
            s0 = sups[0]
            prec_n += 1
            bid, q = s0.get("block_id"), s0.get("quote", "")
            if bid and q and bid in block_map:
                prec_ok += int(_qcov(q, block_map[bid]) >= 0.85)
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
    anchor_precision = round(prec_ok / prec_n, 2) if prec_n else 0.0
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
            "hallucination_rate": round(halluc, 2),
            "anchor_precision": anchor_precision, "anchor_claims": prec_n,
            "best": best[1] if best else None,
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


# ---------- 官方考点 8 问（方案 §2.2；#7/#8 属跨文/语料依赖，由 compare/语料题承接） ----------
# gold 取值来源：v7 原文逐题核对（§5.3 优化器、§3.2.2 多头论证、首页作者行、表 2 BLEU）；
# [11] 为 dynamic-ref——真值从解析产物 references[10] 动态取（同时校验引用解析质量）。

_RUBRIC_SURNAMES = ("vaswani", "shazeer", "uszkoreit", "jones", "gomez", "kaiser", "polosukhin")
_RUBRIC_ORGS = ("google brain", "google research")

_RUBRIC_8 = [
    # (问题, 类别, 校验器)；校验器签名 (answer, docir) -> bool
    ("这篇论文的作者和所属机构？", "作者/机构",
     lambda a, d: any(s in a.lower() for s in _RUBRIC_SURNAMES)
     and any(o in a.lower() for o in _RUBRIC_ORGS)),
    ("β1、β2 设置为多少？", "超参数",
     lambda a, d: "0.9" in a and "0.98" in a),
    ("训练用了什么 GPU、多少块、多久？", "训练事实",
     # base=8×P100 与 big=3.5 天分属两段——GPU+块数 或 GPU+时长 任一组合即认定
     lambda a, d: (_kw(a, "P100") and _kw(a, "8")) or (_kw(a, "P100") and _kw(a, "3.5"))),
    ("在 WMT 2014 EN-DE 上 BLEU 是多少？", "表格数值",
     lambda a, d: _kw(a, "28.4")),
    ("多头注意力为什么比单头好？", "论证链",
     # 答案语言跟随语言边界（默认中文）→ 中英双关键词集合任一语言 ≥2 个要点
     lambda a, d: max(
         sum(1 for kw in ("representation subspace", "different positions", "single attention")
             if kw in a.lower()),
         sum(1 for kw in ("表示子空间", "不同位置", "单头", "平均")
             if kw in a.lower())) >= 2),
    ("[11] 的完整引用信息？", "引用解析",
     lambda a, d: _ref11_check(a, d)),
]


def _kw(answer: str, gold: str) -> bool:
    """token 边界严格匹配：只排除字母数字相邻（"8块" 的 8 合法、"128.4" 不误吞 28.4）。
    不可用 \\w 边界——中文是 \\w 字符，"8块" 的 8 会被误判为词内。"""
    import re as _re
    return _re.search(rf"(?<![A-Za-z0-9]){_re.escape(gold)}(?![A-Za-z0-9])",
                      answer or "", _re.IGNORECASE) is not None


def _ref11_check(answer: str, docir: dict) -> bool:
    """[11] 完整引用：真值从解析产物 references[10] 动态取（title 前 40 字归一子串）。"""
    refs = docir.get("references", [])
    if len(refs) <= 10:
        return False
    title = (refs[10].get("title") or "").strip()
    if len(title) < 8:
        return False
    norm = re.sub(r"\s+", " ", title[:40]).lower()
    return norm in re.sub(r"\s+", " ", answer or "").lower()


def rubric_metrics(client, paper_id: str, docir: dict) -> dict:
    """官方考点 8 问（单篇 6 问；#7 BERT 对比与 #8 GPT 语料题由 compare_metrics/语料承接）。"""
    results = []
    for q, cat, scorer in _RUBRIC_8:
        final, _ = qa_round(client, paper_id, q)
        ans = final.get("answer", "")
        ok = bool(final) and scorer(ans, docir)
        results.append({"question": q, "category": cat, "pass": ok,
                        "mode": final.get("mode"),
                        "answer_excerpt": ans[:200]})
        print(f"  [RUBRIC] {'✓' if ok else '✗'} [{cat}] {q[:36]}")
    n = len(results)
    return {"questions": results, "pass_count": sum(1 for r in results if r["pass"]),
            "total": n, "pass_rate": round(sum(1 for r in results if r["pass"]) / n, 2) if n else 0.0}


def compare_metrics(client) -> dict:
    """多篇对比端到端（P0 新增）：v7+BERT 双篇 forced compare → 表格/格级 E#/覆盖/对称断言。"""
    from app import db
    rows = db.query("SELECT id,title FROM papers WHERE status='active' AND docir_path IS NOT NULL "
                    "AND docir_path != ''")
    att = next((r["id"] for r in rows if "attention" in (r["title"] or "").lower()), "")
    bert = next((r["id"] for r in rows if "bert" in (r["title"] or "").lower()), "")
    if not att or not bert:
        return {"ok": False, "skipped": "缺少对比语料（先跑 scripts/fetch_corpus.py 收录 BERT）"}
    conv = f"gold_cmp_{uuid.uuid4().hex[:8]}"
    final = None
    with client.stream("POST", "/api/chat",
                       json={"question": "对比这两篇论文的方法承袭与核心差异（如预训练目标、上下文表示）",
                             "paper_ids": [att, bert], "conversation_id": conv,
                             "agent": "compare"}) as r:
        assert r.status_code == 200, r.text
        event = ""
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: ") and event:
                d = json.loads(line[6:])
                if event == "final":
                    final = d
    final = final or {}
    table = final.get("compare_table") or {}
    rows_t = table.get("rows", [])
    cells = [c for r2 in rows_t for c in r2.get("cells", [])]
    found = [c for c in cells if c.get("found")]
    found_with_eid = [c for c in found if c.get("eid")]
    coverage = round(len(found) / len(cells), 2) if cells else 0.0
    eid_cov = round(len(found_with_eid) / len(found), 2) if found else 0.0
    claims = final.get("claims", [])
    # 口径：幻觉率（unsupported 占比）≤0.25——compare 跨文综合的 needs_check（数值核对黄标）
    # 不视为幻觉（与单篇 hallucination_rate 同式，阈值按跨文综合难度校准）
    halluc = (sum(1 for c in claims if c.get("status") == "unsupported") / len(claims)
              if claims else 1.0)
    answer = final.get("answer", "")
    p_labels = "P1" in answer and "P2" in answer
    one_sided = any(str(g).startswith("compare_one_sided") for g in final.get("guardrails", []))
    ok = (bool(rows_t) and len(rows_t) >= 2 and coverage >= 0.5 and eid_cov >= 0.6
          and halluc <= 0.25 and p_labels and not one_sided)
    detail = {"ok": ok, "rows": len(rows_t), "cells": len(cells), "found": len(found),
              "coverage": coverage, "eid_coverage": eid_cov,
              "claims": len(claims), "unsupported_ratio": round(halluc, 2),
              "p_labels": p_labels, "one_sided": one_sided,
              "guardrails": final.get("guardrails", [])}
    print(f"  [CMP] {'✓' if ok else '✗'} rows={len(rows_t)} found={len(found)}/{len(cells)} "
          f"eid={eid_cov} halluc={halluc} P1P2={p_labels}")
    return detail


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
        from app.rag.version_diff import load_docir_for
        docir = load_docir_for(paper_id) or {}
        gen = generative_metrics(client, paper_id)
        report["generative"] = {k: v for k, v in gen.items() if k != "best"}
        if gen["best"]:
            report["generative_trace"] = gen["best"]
        report["replay_race"] = replay_race(client, paper_id)
        report["translate"] = translate_evidence(client, paper_id)
        report["rubric"] = rubric_metrics(client, paper_id, docir)
        report["compare"] = compare_metrics(client)

    gates = {
        "recall_at5>=0.8": report.get("recall_at5", 0) >= 0.8,
        # Round3 收紧：严格口径（token 边界命中）进门禁——宽口径会掩盖子串/粘连误命中
        "strict_recall_at5>=0.7": report.get("strict_recall_at5", 0) >= 0.7,
        "refusal>=0.9": report.get("refusal_accuracy", 0) >= 0.9,
        "zh_top8>=0.8": report.get("zh_top8_hit", 0) >= 0.8,
    }
    if not args.skip_llm:
        gates["supported>=0.8"] = report["generative"]["supported_ratio"] >= 0.8
        gates["hallucination<=0.1"] = report["generative"]["hallucination_rate"] <= 0.1
        gates["replay_10_10"] = report["replay_race"]["ok"] == report["replay_race"]["rounds"]
        gates["anchor_precision>=0.7"] = report["generative"].get("anchor_precision", 0) >= 0.7
        gates["rubric8>=0.75"] = report.get("rubric", {}).get("pass_rate", 0) >= 0.75
        gates["compare_ok"] = bool(report.get("compare", {}).get("ok"))
        # Round3 收紧：compare 细分指标独立进门禁（旧口径只进一个端到端布尔）
        cmp_ = report.get("compare", {})
        if isinstance(cmp_, dict) and not cmp_.get("skipped"):
            gates["compare_cell_coverage>=0.5"] = float(cmp_.get("coverage", 0)) >= 0.5
            gates["compare_eid_coverage>=0.6"] = float(cmp_.get("eid_coverage", 0)) >= 0.6
            gates["compare_unsupported<=0.25"] = float(cmp_.get("unsupported_ratio", 1)) <= 0.25
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
