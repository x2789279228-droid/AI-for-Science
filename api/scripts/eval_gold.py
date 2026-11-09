"""金标评测 + 证据采集（验收方案 §6 + Round4 评测硬化）。

需真实 LLM（读取真实 .env；引擎开）：对库内论文跑
- 英文事实 10 题：Recall@5 宽口径（gate）+ 严格口径（alnum 边界+版式归一+多关键词
  金标集合）+ MRR@5（报告字段）
- 官方考点 8 问（方案 §2.2，Round4 起真 8 问）：作者机构/β1β2/GPU/BLEU/论证链/[11] 引用
  + #7 Attention↔BERT 对比（复用 compare 题①答案）+ #8 GPT-3 语料题 → rubric8 ≥0.75
- 应拒答 5 题 ≥0.9；中文 5 题 top-8 ≥0.8
- 生成式（Round4 扩到 12 题）：supported ≥80%、hallucination ≤10%、anchor_precision
  （**全部 verified support** 块级锚点存在且 quote 覆盖 ≥0.85）≥0.85 且分母 ≥15、
  引文未验证率 ≤0.15、回放 10/10
- 多篇对比（Round4 手工金标）：3 题 × Attention/BERT 矩阵——含**缺失格**（BERT 无
  WMT14 BLEU，须如实未报告+零编造）与**需限定格**（BERT 参数/层数须分 Base/Large）；
  cell 级溯源（quote 归一覆盖于 block_ids）≥0.8、unsupported ≤0.15

用法：cd api && uv run python scripts/eval_gold.py --out ../../docs/acceptance/round4-<date>/
前置：库内有已解析论文（对比/GPT-3 题需先跑 scripts/fetch_corpus.py 收录 BERT+GPT-3）；进程内直调。
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
    # (问题, 金标关键词集合)；金标词取自真实论文（Attention Is All You Need）正文。
    # Round4：单金标词 → 多关键词集合（任一变体严格命中即算命中）。变体只允许
    # 论文真实用语（如 "constant number of sequentially executed operations" 的
    # constant / "six"/"N = 6" 的 six），不许靠放宽边界规则凑命中。
    ("How many GPUs were the base models trained on?", ("P100",)),
    ("How long did the big model train for?", ("3.5",)),
    ("What BLEU score did the big model achieve on EN-DE?", ("28.4",)),
    ("What attention mechanism computes in constant memory?", ("length", "constant")),
    ("Which optimizer was used?", ("Adam",)),
    ("What learning rate schedule was applied?", ("warmup",)),
    ("What is the dropout applied to the sum of embeddings?", ("residual",)),
    ("How many encoder and decoder layers?", ("6", "six")),
    ("What is the model dimension d_model?", ("512",)),
    ("Which regularization prevented overfitting?", ("dropout",)),
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


# Round4 严格口径归一：两类解析版式伪影会让严格边界假阴性——
#   ① 数字间空格：MinerU 逐字符定位会把 "512" 重建为 "= 5 1 2"（round3 实证）
#   ② 数字字母粘连：bbox 重建丢空格产生 "6identical"（round3 实证）
# 只压缩数字间隙、拆开数字→字母粘连，不动其它空白；gold 侧同步归一保证对称。
_DIGIT_GAP = re.compile(r"(?<=\d)\s+(?=\d)")
_DIGIT_ALPHA_GLUE = re.compile(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)")


def _norm_search_text(t: str) -> str:
    t = _DIGIT_GAP.sub("", t or "")
    return _DIGIT_ALPHA_GLUE.sub(" ", t)


def _strict_hit(golds: tuple[str, ...], text: str) -> bool:
    """严格命中：任一金标变体在归一化文本中以 alnum 边界出现。

    边界用 (?<![A-Za-z0-9])…(?![A-Za-z0-9])（与 _kw 同款；中文是 \\w 字符，
    \\w 边界会把 "8块" 的 8 误判词内 → round3 假阴性来源之一）。
    "warmup" 命中 "warmup_steps" 属合法（_ 非 alnum，与答案侧 _kw 口径一致）。
    """
    t = _norm_search_text(text)
    return any(re.search(rf"(?<![A-Za-z0-9]){re.escape(g)}(?![A-Za-z0-9])", t, re.IGNORECASE)
               is not None for g in golds)


def retrieval_metrics(paper_id: str) -> dict:
    """检索指标（不依赖 LLM）：宽口径 Recall@5（gate）+ 严格口径（token 边界）+ MRR@5。"""
    from app.rag.indexing import hybrid_search

    r5_hits, r5_strict, r5_rr, r5_detail = [], [], [], []
    for q, golds in _EN_FACTS:
        res = hybrid_search([paper_id], q, top_k=5)
        hits = res.get("hits", [])
        ok = any(g.lower() in (h.get("text") or "").lower() for g in golds for h in hits)
        strict = any(_strict_hit(golds, h.get("text") or "") for h in hits)
        rank = next((i + 1 for i, h in enumerate(hits)
                     if any(g.lower() in (h.get("text") or "").lower() for g in golds)), 0)
        r5_hits.append(ok)
        r5_strict.append(strict)
        r5_rr.append(1.0 / rank if rank else 0.0)
        r5_detail.append({
            "question": q, "gold_keywords": list(golds), "hit": ok, "strict_hit": strict,
            "first_hit_rank": rank,
            "top5": [{"chunk_id": h.get("chunk_id"), "page": (h.get("page") or 0) + 1,
                      "score": h.get("rrf") or h.get("score"),
                      "kind": h.get("kind"),
                      "table_boosted": bool(h.get("table_boosted")),
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
    """生成式：claims supported 占比 / 锚点一致性（support 级全量）/ 端到端耗时；选最优导出 trace。"""
    from app.rag.verify import _norm as _norm_txt
    from app.rag.version_diff import load_docir_for

    claims_stat = {"total": 0, "supported": 0, "needs_check": 0, "unsupported": 0}
    # 锚点一致性（Round4 修订）：全部 verified support 的块级锚点存在且 quote 覆盖 ≥0.85
    block_map: dict[str, str] = {}
    docir = load_docir_for(paper_id) or {}
    for b in docir.get("blocks", []):
        block_map[b.get("id", "")] = _norm_txt(b.get("text", ""))
    prec_n, prec_ok = 0, 0
    quote_stat = {"claims": 0, "unverified": 0}
    anchor_detail: list[dict] = []
    best = None
    # Round4：5 题 → 全部 10 英文题 + 2 中文题（扩大 claims 样本，anchor 分母 ≥15）
    from app.rag.verify import quote_coverage as _qcov
    gen_qs = [q for q, _ in _EN_FACTS] + [q for q, _ in _ZH_FACTS]
    for q in gen_qs:
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
            if c.get("quote_status") is not None:
                quote_stat["claims"] += 1
                quote_stat["unverified"] += int(c.get("quote_status") != "verified")
        # anchor_precision（Round4 口径收紧）：分母=**全部 verified support**（不再只看
        # 首 support）——评审要求「全部 verified support 有 block_id/bbox 且一致」；
        # 命中=有块级锚点且 quote 对锚块（跨块锚点取拼接文本）覆盖率 ≥0.85。
        # 转述/拼凑引文（unverified）不定位也不计入分母（其诚实信号是徽章）
        for c in claims:
            if c.get("quote_status") != "verified":
                continue
            for s in c.get("support", []):
                bid, quote = s.get("block_id"), s.get("quote", "")
                prec_n += 1
                if not bid or not quote:
                    anchor_detail.append({"q": q[:30], "block_id": bid, "pass": False,
                                          "reason": "no_block_anchor"})
                    continue
                texts = [block_map.get(b, "") for b in (s.get("block_ids") or [bid])]
                text = _norm_txt(" ".join(t for t in texts if t))
                ok = bid in block_map and _qcov(quote, text) >= 0.85
                prec_ok += int(ok)
                anchor_detail.append({"q": q[:30], "block_id": bid,
                                      "block_span": bool(s.get("block_span")), "pass": bool(ok)})
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
    quote_unverified_rate = round(quote_stat["unverified"] / quote_stat["claims"], 2) \
        if quote_stat["claims"] else 0.0
    # trace 补 usage/model（来自 qa_logs 最新流式行；验收 A5：无 usage = 证据不全）
    usage_row = db.query("SELECT model, prompt_tokens, completion_tokens, latency_ms "
                         "FROM qa_logs WHERE guardrail='stream' ORDER BY id DESC LIMIT 1")
    usage = dict(usage_row[0]) if usage_row else {}
    if best is not None and best[1] is not None and usage:
        best[1]["model"] = usage.get("model", "")
        best[1]["usage"] = {"prompt_tokens": usage.get("prompt_tokens", 0),
                            "completion_tokens": usage.get("completion_tokens", 0),
                            "latency_ms": usage.get("latency_ms", 0)}
    return {"claims": claims_stat, "supported_ratio": round(sup_ratio, 2),
            "hallucination_rate": round(halluc, 2),
            "anchor_precision": anchor_precision, "anchor_claims": prec_n,
            "quote_unverified_rate": quote_unverified_rate,
            "anchor_detail_sample": anchor_detail[:30],
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


# ---------- 官方考点 8 问（方案 §2.2）----------
# Round4 修正：此前 _RUBRIC_8 实际只有 6 问（#7/#8 靠 docstring 声明由 compare/语料
# 承接，total=6 却用 rubric8 名义出门禁）——评审指出名不副实。现在补齐为真 8 问：
#   #7 对比题：复用 compare 金标题组①的答案（省一次跨文调用），确定性关键词断言
#   #8 GPT-3 语料题：对库内 GPT-3 论文真实问答（需 fetch_corpus 收录 2005.14165）
# gold 取值来源：v7 原文逐题核对（§5.3 优化器、§3.2.2 多头论证、首页作者行、表 2 BLEU）；
# [11] 为 dynamic-ref——真值从解析产物 references[10] 动态取（同时校验引用解析质量）。

_RUBRIC_SURNAMES = ("vaswani", "shazeer", "uszkoreit", "jones", "gomez", "kaiser", "polosukhin")
_RUBRIC_ORGS = ("google brain", "google research")

_RUBRIC_8 = [
    # (问题, 类别, 模式, 校验器)；校验器签名 (answer, docir) -> bool；
    # 模式 single=对主论文问答；compare_q1=复用 compare 金标题组①答案；gpt3=对库内 GPT-3 论文问答
    {"q": "这篇论文的作者和所属机构？", "cat": "作者/机构", "mode": "single",
     "check": lambda a, d: any(s in a.lower() for s in _RUBRIC_SURNAMES)
     and any(o in a.lower() for o in _RUBRIC_ORGS)},
    {"q": "β1、β2 设置为多少？", "cat": "超参数", "mode": "single",
     "check": lambda a, d: "0.9" in a and "0.98" in a},
    {"q": "训练用了什么 GPU、多少块、多久？", "cat": "训练事实", "mode": "single",
     # base=8×P100 与 big=3.5 天分属两段——GPU+块数 或 GPU+时长 任一组合即认定
     "check": lambda a, d: (_kw(a, "P100") and _kw(a, "8")) or (_kw(a, "P100") and _kw(a, "3.5"))},
    {"q": "在 WMT 2014 EN-DE 上 BLEU 是多少？", "cat": "表格数值", "mode": "single",
     "check": lambda a, d: _kw(a, "28.4")},
    {"q": "多头注意力为什么比单头好？", "cat": "论证链", "mode": "single",
     # 答案语言跟随语言边界（默认中文）→ 中英双关键词集合任一语言 ≥2 个要点
     "check": lambda a, d: max(
         sum(1 for kw in ("representation subspace", "different positions", "single attention")
             if kw in a.lower()),
         sum(1 for kw in ("表示子空间", "不同位置", "单头", "平均")
             if kw in a.lower())) >= 2},
    {"q": "[11] 的完整引用信息？", "cat": "引用解析", "mode": "single",
     "check": lambda a, d: _ref11_check(a, d)},
    {"q": "本文（Attention）与 BERT 在预训练目标与上下文表示上的核心差异？", "cat": "对比（BERT）",
     "mode": "compare_q1",
     # BERT：MLM/掩码 + 双向；Attention：无预训练、自回归/编码器-解码器或 left-to-right 解码
     "check": lambda a, d: (
         any(k in a.lower() for k in ("mask", "mlm", "掩码", "完形"))
         and any(k in a.lower() for k in ("bidirectional", "双向", "left-to-right",
                                          "自回归", "单向", "encoder-decoder", "编码器-解码器"))
         and ("P1" in a and "P2" in a))},
    {"q": "GPT-3 的 few-shot / in-context learning 指什么？", "cat": "语料（GPT-3）",
     "mode": "gpt3",
     # 事实：通过自然语言提示/示例上下文学习，无需梯度更新或微调（GPT-3 §2/§3.1）
     "check": lambda a, d: (
         any(k in a.lower() for k in ("few-shot", "in-context", "上下文学习", "少样本"))
         and any(k in a.lower() for k in ("无需微调", "不进行微调", "无需梯度", "不更新参数",
                                          "without fine-tuning", "no fine-tuning",
                                          "without gradient", "without parameter updates",
                                          "without any gradient")))},
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


def _find_corpus_paper(keyword: str) -> str:
    """按标题关键词找库内已解析论文（GPT-3 语料题用；找不到返回空串）。"""
    from app import db
    rows = db.query("SELECT id, title FROM papers WHERE status='active' AND docir_path IS NOT NULL "
                    "AND docir_path != ''")
    for r in rows:
        if keyword in (r["title"] or "").lower():
            return r["id"]
    return ""


def rubric_metrics(client, paper_id: str, docir: dict, compare_answer: str = "") -> dict:
    """官方考点 8 问（Round4 起为真 8 问：#7 复用 compare 题①答案，#8 对 GPT-3 语料问答）。"""
    results = []
    for spec in _RUBRIC_8:
        q, cat, mode, scorer = spec["q"], spec["cat"], spec.get("mode", "single"), spec["check"]
        note = ""
        if mode == "compare_q1":
            ans = compare_answer
            ok = bool(ans) and scorer(ans, docir)
            if not ans:
                note = "compare 答案缺失"
        elif mode == "gpt3":
            pid = _find_corpus_paper("few-shot learners")
            if not pid:
                ans, ok = "", False
                note = "语料缺失（需 fetch_corpus 收录 GPT-3 2005.14165）"
            else:
                final, _ = qa_round(client, pid, q)
                ans = final.get("answer", "")
                ok = bool(final) and scorer(ans, docir)
        else:
            final, _ = qa_round(client, paper_id, q)
            ans = final.get("answer", "")
            ok = bool(final) and scorer(ans, docir)
        results.append({"question": q, "category": cat, "mode": mode, "pass": ok,
                        "answer_excerpt": ans[:200], **({"note": note} if note else {})})
        print(f"  [RUBRIC] {'✓' if ok else '✗'} [{cat}] {q[:36]}" + (f"（{note}）" if note else ""))
    n = len(results)
    return {"questions": results, "pass_count": sum(1 for r in results if r["pass"]),
            "total": n, "pass_rate": round(sum(1 for r in results if r["pass"]) / n, 2) if n else 0.0}


# ---------- Compare 金标（Round4：3 题手工标注矩阵 + 缺失格诚实 + cell 级溯源） ----------
# 语料：Attention(P1) vs BERT(P2)（paper_ids 顺序决定 P 标签）。金标值依据两篇原文核实：
#   ① 预训练目标/上下文表示：Attention 端到端翻译训练（无预训练）/编码器-解码器、
#      解码器单向自回归；BERT 掩码语言模型（MLM）+ 双向表示。
#   ② WMT14 EN-DE BLEU：Attention big=28.4（表 2）；BERT 原文不报告该指标 → **缺失格**，
#      必须如实「未报告/未提及」；把任何数值安在 BERT/P2 头上即编造（正则守卫）。
#   ③ 层数/参数量：Attention N=6；BERT 必须限定 Base(12 层/110M)/Large(24 层/340M)——
#      不限定版本的单一数值不通过（版本歧义是数值幻觉高发区）。

_MISSING_PHRASES = ("未报告", "未提及", "没有报告", "未提供", "未给出", "原文没有",
                    "没有给出", "not report", "no reported", "not mention", "未找到")

_COMPARE_GOLD = [
    {"q": "对比这两篇论文的预训练目标与上下文表示方式的核心差异",
     "aspects": ("预训练", "训练目标", "上下文", "表示", "pretrain", "context", "目标", "方向"),
     "cells": {
         "P1": {"expect": "found",
                "any": ("自回归", "encoder-decoder", "编码器-解码器", "left-to-right",
                        "seq2seq", "端到端", "机器翻译", "无预训练", "end-to-end", "单向")},
         "P2": {"expect": "found",
                "any": ("掩码", "mask", "mlm", "双向", "bidirectional", "完形")},
     }},
    {"q": "对比这两篇论文在 WMT 2014 英德翻译任务上的 BLEU 分数",
     "aspects": ("BLEU", "翻译", "WMT"),
     "cells": {
         "P1": {"expect": "found", "all": ("28.4",)},
         "P2": {"expect": "missing"},
     },
    "fabrication_patterns": [
        r"(?:P2|BERT)[^\n。]{0,60}?BLEU[^\n。]{0,25}?(?<![A-Za-z0-9])\d+(?:\.\d+)?",
        r"BLEU[^\n。]{0,25}?(?<![A-Za-z0-9])\d+(?:\.\d+)?[^\n。]{0,20}?(?:P2|BERT)",
     ]},
    {"q": "对比这两篇论文的模型层数与参数量",
     "aspects": ("层数", "参数", "layer", "param", "规模", "层"),
     "cells": {
         "P1": {"expect": "found", "any": ("6", "six", "213", "65")},
         "P2": {"expect": "found",
                "any": ("110", "340", "12", "24", "Base", "Large", "base", "large"),
                # qualifier 只认版本词：裸数值（"12 层/110M"）不说明 Base/Large 即版本歧义
                "qualifier": ("Base", "Large", "base", "large")},
     }},
]


def _eval_compare_gold(gold: dict, final: dict, labels: list[str]) -> dict:
    """单题金标评估（纯函数，可单测）。

    - found 格：维度行命中 + (answer+cell.quote) 含金标词（any/all）+ qualifier 在答案中
    - missing 格：cell 未 found 或 answer 明说未报告；且 fabrication 正则未命中
    - 维度行整体缺失 → 该题 found 格全部判不通过（覆盖失败）
    """
    answer = (final or {}).get("answer", "") or ""
    al = answer.lower()
    table = (final or {}).get("compare_table") or {}
    rows = table.get("rows", [])
    matched_rows = [r for r in rows
                    if any(k.lower() in (r.get("dimension") or "").lower()
                           for k in gold["aspects"])]
    cell_evals = []
    missing_total = missing_pass = 0
    fabrication = False
    for pat in gold.get("fabrication_patterns", []):
        if re.search(pat, answer):
            fabrication = True
    for label, cell_gold in gold["cells"].items():
        idx = labels.index(label) if label in labels else -1
        row = matched_rows[0] if matched_rows else {}
        cells = row.get("cells", [])
        cell = cells[idx] if 0 <= idx < len(cells) else {}
        found = bool(cell.get("found"))
        quote = (cell.get("quote") or "").lower()
        expect = cell_gold["expect"]
        if expect == "found":
            hay = al + " " + quote
            if "all" in cell_gold:
                ok = all(k.lower() in hay for k in cell_gold["all"])
            else:
                ok = any(k.lower() in hay for k in cell_gold["any"])
            if "qualifier" in cell_gold:
                ok = ok and any(k.lower() in al for k in cell_gold["qualifier"])
            ok = ok and bool(matched_rows)
            cell_evals.append({"label": label, "expect": "found", "pass": bool(ok),
                               "row_matched": bool(matched_rows), "cell_found": found})
        else:
            missing_total += 1
            honest = (not found) or any(p in answer or p.lower() in al for p in _MISSING_PHRASES)
            ok = honest and not fabrication
            missing_pass += int(bool(ok))
            cell_evals.append({"label": label, "expect": "missing", "pass": bool(ok),
                               "cell_found": found, "honest_phrase": bool(honest),
                               "fabrication": fabrication})
    return {"gold_cells": cell_evals,
            "gold_pass": sum(1 for c in cell_evals if c["pass"]),
            "gold_total": len(cell_evals),
            "missing_total": missing_total, "missing_pass": missing_pass,
            "fabrication": fabrication}


def _compare_round(client, att: str, bert: str, question: str) -> dict:
    """单次 forced-compare 问答，返回 final 事件。"""
    conv = f"gold_cmp_{uuid.uuid4().hex[:8]}"
    final = None
    with client.stream("POST", "/api/chat",
                       json={"question": question, "paper_ids": [att, bert],
                             "conversation_id": conv, "agent": "compare"}) as r:
        assert r.status_code == 200, r.text
        event = ""
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: ") and event:
                d = json.loads(line[6:])
                if event == "final":
                    final = d
    return final or {}


def compare_metrics(client) -> dict:
    """多篇对比端到端（Round4：3 题手工金标矩阵 + 缺失格零编造 + cell 级溯源校验）。"""
    from app import db
    from app.rag.verify import _norm as _norm_txt
    from app.rag.verify import quote_coverage as _qcov
    from app.rag.version_diff import load_docir_for

    rows = db.query("SELECT id,title FROM papers WHERE status='active' AND docir_path IS NOT NULL "
                    "AND docir_path != ''")
    att = next((r["id"] for r in rows if "attention" in (r["title"] or "").lower()), "")
    bert = next((r["id"] for r in rows if "bert" in (r["title"] or "").lower()), "")
    if not att or not bert:
        return {"ok": False, "skipped": "缺少对比语料（先跑 scripts/fetch_corpus.py 收录 BERT）"}
    labels = ["P1", "P2"]
    pid_of = {"P1": att, "P2": bert}
    bmaps: dict[str, dict[str, str]] = {}
    for lab, pid in pid_of.items():
        bmaps[lab] = {b.get("id", ""): _norm_txt(b.get("text", ""))
                      for b in (load_docir_for(pid) or {}).get("blocks", [])}

    tot_cells = found_cells = found_eid = 0
    gold_pass = gold_total = missing_total = missing_pass = 0
    cq_total = cq_ok = 0          # cell 级溯源：quote 归一覆盖于其 block_ids 拼接文本
    claims_total = unsupported = quote_claims = quote_unverified = 0
    fabrication_any = False
    all_ok = True
    qs_detail = []
    for gold in _COMPARE_GOLD:
        final = _compare_round(client, att, bert, gold["q"])
        table = final.get("compare_table") or {}
        rows_t = table.get("rows", [])
        cells = [c for r2 in rows_t for c in r2.get("cells", [])]
        found = [c for c in cells if c.get("found")]
        tot_cells += len(cells)
        found_cells += len(found)
        found_eid += sum(1 for c in found if c.get("eid"))
        for i, c in enumerate(found):
            lab = labels[i % len(labels)]
            bids = c.get("block_ids") or []
            if not bids or not c.get("quote"):
                continue
            text = _norm_txt(" ".join(bmaps[lab].get(b, "") for b in bids))
            if not text:
                continue
            cq_total += 1
            cq_ok += int(_qcov(c["quote"], text) >= 0.85)
        claims = final.get("claims", [])
        claims_total += len(claims)
        unsupported += sum(1 for c in claims if c.get("status") == "unsupported")
        for c in claims:
            if c.get("quote_status") is not None:
                quote_claims += 1
                quote_unverified += int(c.get("quote_status") != "verified")
        ev = _eval_compare_gold(gold, final, labels)
        gold_pass += ev["gold_pass"]
        gold_total += ev["gold_total"]
        missing_total += ev["missing_total"]
        missing_pass += ev["missing_pass"]
        fabrication_any = fabrication_any or ev["fabrication"]
        p_labels = "P1" in (final.get("answer") or "") and "P2" in (final.get("answer") or "")
        one_sided = any(str(g).startswith("compare_one_sided") for g in final.get("guardrails", []))
        q_ok = (bool(rows_t) and ev["gold_pass"] == ev["gold_total"]
                and p_labels and not one_sided)
        all_ok = all_ok and q_ok
        qs_detail.append({"question": gold["q"], "ok": bool(q_ok), "rows": len(rows_t),
                          "cells": len(cells), "found": len(found),
                          "p_labels": p_labels, "one_sided": one_sided, "gold_eval": ev,
                          "answer_full": final.get("answer") or "",
                          "answer_excerpt": (final.get("answer") or "")[:400],
                          "table_excerpt": rows_t[:3],
                          "guardrails": final.get("guardrails", [])})
        print(f"  [CMP] {'✓' if q_ok else '✗'} gold={ev['gold_pass']}/{ev['gold_total']} "
              f"rows={len(rows_t)} found={len(found)}/{len(cells)} P1P2={p_labels}")
    coverage = round(found_cells / tot_cells, 2) if tot_cells else 0.0
    eid_cov = round(found_eid / found_cells, 2) if found_cells else 0.0
    unsupported_ratio = round(unsupported / claims_total, 2) if claims_total else 1.0
    quote_unverified_rate = round(quote_unverified / quote_claims, 2) if quote_claims else 0.0
    gold_cell_acc = round(gold_pass / gold_total, 2) if gold_total else 0.0
    missing_honest = round(missing_pass / missing_total, 2) if missing_total else 1.0
    cell_quote = round(cq_ok / cq_total, 2) if cq_total else 0.0
    ok = (all_ok and not fabrication_any and coverage >= 0.5 and eid_cov >= 0.6
          and unsupported_ratio <= 0.15)
    return {"ok": bool(ok), "questions": len(_COMPARE_GOLD), "rows": None,
            "cells": tot_cells, "found": found_cells,
            "coverage": coverage, "eid_coverage": eid_cov,
            "claims": claims_total, "unsupported_ratio": unsupported_ratio,
            "quote_unverified_rate": quote_unverified_rate,
            "gold_cell_acc": gold_cell_acc,
            "missing_honest": missing_honest,
            "cell_quote_provenance": cell_quote,
            "fabrication": fabrication_any,
            "questions_detail": qs_detail}


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
        # compare 先跑：rubric #7 复用其题①答案（省一次跨文调用）
        cmp_ = compare_metrics(client)
        report["compare"] = cmp_
        cmp_q1_answer = (cmp_.get("questions_detail") or [{}])[0].get("answer_full", "") \
            if not cmp_.get("skipped") else ""
        report["rubric"] = rubric_metrics(client, paper_id, docir, compare_answer=cmp_q1_answer)

    gates = {
        "recall_at5>=0.8": report.get("recall_at5", 0) >= 0.8,
        # Round4：严格口径换 alnum 边界+版式归一（数字空格/数字字母粘连）+多关键词金标集合
        "strict_recall_at5>=0.7": report.get("strict_recall_at5", 0) >= 0.7,
        "refusal>=0.9": report.get("refusal_accuracy", 0) >= 0.9,
        "zh_top8>=0.8": report.get("zh_top8_hit", 0) >= 0.8,
    }
    if not args.skip_llm:
        gates["supported>=0.8"] = report["generative"]["supported_ratio"] >= 0.8
        gates["hallucination<=0.1"] = report["generative"]["hallucination_rate"] <= 0.1
        gates["replay_10_10"] = report["replay_race"]["ok"] == report["replay_race"]["rounds"]
        # Round4 收紧：anchor 口径改为全 verified support（不再只看首个），阈值 0.7→0.85，
        # 分母 ≥15（生成题 5→12）；引文未验证率进门禁（原 guardrail 只报告不设门）
        gates["anchor_precision>=0.85"] = report["generative"].get("anchor_precision", 0) >= 0.85
        gates["anchor_claims>=15"] = report["generative"].get("anchor_claims", 0) >= 15
        gates["gen_quote_unverified<=0.15"] = \
            report["generative"].get("quote_unverified_rate", 1) <= 0.15
        gates["rubric8>=0.75"] = report.get("rubric", {}).get("pass_rate", 0) >= 0.75
        gates["rubric8_total==8"] = report.get("rubric", {}).get("total", 0) == 8
        # Round4：compare 从「动态 2×2 端到端布尔」升级为 3 题手工金标矩阵 +
        # 缺失格零编造 + cell 级溯源；unsupported 门禁 0.25→0.15
        gates["compare_ok"] = bool(report.get("compare", {}).get("ok"))
        if isinstance(cmp_, dict) and not cmp_.get("skipped"):
            gates["compare_gold_cell_acc>=0.8"] = float(cmp_.get("gold_cell_acc", 0)) >= 0.8
            gates["compare_missing_honest==1.0"] = float(cmp_.get("missing_honest", 0)) >= 1.0
            gates["compare_cell_quote>=0.8"] = float(cmp_.get("cell_quote_provenance", 0)) >= 0.8
            gates["compare_unsupported<=0.15"] = float(cmp_.get("unsupported_ratio", 1)) <= 0.15
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
