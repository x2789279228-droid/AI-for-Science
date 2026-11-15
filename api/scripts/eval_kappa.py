"""LLM-Judge 校准双 κ（Round6-P1）：judge-only κ + 全管线 κ，各自 vs 构造真值。

样本设计（50 条平衡，构造即真值——无需人工标注）：
- 正类：R3 金标评测真实 claims（status=supported，引文逐字来自证据）→ 真值=supported
- 负类：程序构造的三种「看似合理实则无支撑」：
  a. 数值篡改（28.4→28.9 等） b. 实体替换（P100→V100、Adam→SGD）
  c. 论断-证据错配（claim 配另一篇/另一段的证据）
  → 真值=unsupported

两种口径（诚实分离，不再混测）：
- judge-only κ：仅 L4 语义判定器（verdict 读 semantic_status，不读 final status——
  旧实现把 needs_check 初始态 claims 直接灌给 judge，yes 判定不升级 status，
  全样本读成 flagged → κ=0.0 是度量伪影而非 judge 无判别力）
- pipeline κ：L2 引文→L3 数值→L4 语义全链路（final status 二值化），度量端到端防御

分块：每批 ≤6 条、批内证据包独立编号——生产 judge 调用证据包 ≤6000 字符；
旧实现 38 条证据挤一个包被截断，后半样本 judge 看不到证据（第二个失真源）。

用法：cd api && uv run python scripts/eval_kappa.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.verify import verify_claims

_GOLD_R3 = Path(__file__).resolve().parents[2] / "docs" / "acceptance" / "eval-20261114-r3" / "eval_gold.json"
_BATCH = 6          # 每批样本数——批证据包 6×1200 字符 ≈ 生产 judge 包规模


def _load_positive_claims() -> list[dict]:
    """实时生成收集 supported claims（text/quote/support 齐全的）——比读历史
    trace 更新鲜且数量可控；走与生产一致的 chat 管线。"""
    import time as _time

    from fastapi.testclient import TestClient

    from app import db as _db
    from app.main import app as _app
    _db.init_db()
    client = TestClient(_app)
    rows = _db.query("SELECT id FROM papers WHERE docir_path!='' AND "
                     "COALESCE(doc_type,'paper')='paper' ORDER BY created_at DESC")
    pid = ""
    for r in rows:
        t = (_db.query("SELECT title FROM papers WHERE id=?", (r["id"],))[0]["title"] or "").lower()
        if "attention is all you need" in t:
            pid = r["id"]; break
    pid = pid or (rows[0]["id"] if rows else "")
    out: list[dict] = []
    qs = ["How many GPUs were the base models trained on?",
          "What BLEU score did the big model achieve on EN-DE?",
          "Which optimizer was used?",
          "What is the model dimension d_model?",
          "How many encoder layers are there?",
          "What dropout value is applied to each sub-layer?",
          "How many attention heads does the model use?",
          "What positional encoding does the model use?"]
    for q in qs:
        conv = f"kappa_{_time.time_ns()}"
        final = None
        event = ""
        buf = b""
        sep = b"\n\n"
        with client.stream("POST", "/api/chat", json={
                "question": q, "paper_ids": [pid], "conversation_id": conv,
                "agent": "reader", "doc_mode": "paper"}) as r:
            for chunk in r.iter_bytes():
                buf += chunk
                while sep in buf:
                    frame, buf = buf.split(sep, 1)
                    for line in frame.decode("utf-8", "replace").splitlines():
                        if line.startswith("event: "):
                            event = line[7:].strip()
                        elif line.startswith("data: ") and event == "final":
                            final = json.loads(line[6:])
                    if event == "done":
                        break
                if event == "done":
                    break
        final = final or {}
        for c in final.get("claims") or []:
            sups = c.get("support") or []
            if not sups:
                continue
            s0 = sups[0]
            if not (c.get("text") and s0.get("quote")):
                continue
            out.append({"text": c["text"], "quote": s0["quote"],
                        "support_ref": {"eid": s0.get("eid", "E1")}})
    print(f"实时收集 supported/候选 claims: {len(out)}")
    return out


def _build_negatives(claims: list[dict]) -> list[dict]:
    """三类程序负类：数值篡改 / 实体替换 / 论断-证据错配。

    P1 构造修正（首轮实测 8/8 FP 均为伪影，诚实修正样本而非宽松化判定）：
    - 数值/实体篡改必须落在 **claim 文本**（judge 的输入只有 claim+证据，quote 篡改
      judge 不可见——那是 L2/L3 机械层的职责；旧构造把篡改放 quote，judge 判 yes
      在语义上正确，被误计 FP）；
    - 错配负类的证据取 **配对 claim 的证据文本**（旧实现按 quote 反查，找回 claim
      自己的正确证据 → 样本自洽，judge 判 yes 同样正确，被误计 FP）。"""
    negs: list[dict] = []
    # a. 数值篡改：claim 文本含数字才可构造（篡改同步落 text 与 quote）
    num_pairs = [c for c in claims if re.findall(r"\d+\.\d+|\d+", c["text"])]
    for c in num_pairs[:8]:
        t2, q2 = c["text"], c["quote"]
        tampered = None
        for num in re.findall(r"\d+\.\d+|\d+", t2):
            tampered = str(int(num) + 7) if "." not in num else f"{float(num) + 0.5:.1f}"
            t2 = t2.replace(num, tampered, 1)
            q2 = q2.replace(num, tampered, 1)
            break
        negs.append({"text": t2, "quote": q2, "eid": c["support_ref"]["eid"],
                     "support_ref": c["support_ref"], "neg_kind": f"numeric:{num}->{tampered}"})
    # b. 实体替换：实体必须出现在 claim 文本（judge 可见）
    ent_pairs = [c for c in claims
                 if re.search(r"\b(Adam|P100|V100|Transformer|Encoder|Decoder|ReLU)\b", c["text"])]
    swap = {"Adam": "SGD", "P100": "V100", "Transformer": "LSTM"}
    for c in ent_pairs[:6]:
        t2, q2 = c["text"], c["quote"]
        hit = None
        for e, r in swap.items():
            if e in t2:
                t2 = t2.replace(e, r, 1)
                q2 = q2.replace(e, r, 1)
                hit = f"{e}->{r}"
                break
        if hit:
            negs.append({"text": t2, "quote": q2, "eid": c["support_ref"]["eid"],
                         "support_ref": c["support_ref"], "neg_kind": f"entity:{hit}"})
    # c. 论断-证据错配：claim i 的 text+quote 配 claim i+2 的证据文本
    #    （错配证据在主流程用 evidence_for(b.quote) 反查——b 的 quote 找 b 的块）
    for i in range(min(6, len(claims) - 2)):
        a, b = claims[i], claims[i + 2]
        negs.append({"text": a["text"], "quote": a["quote"], "eid": b["support_ref"]["eid"],
                     "support_ref": {**b["support_ref"], "_evidence_quote": b["quote"]},
                     "neg_kind": "mismatch"})
    return negs[:20]


def _kappa(pairs: list[tuple[str, str]]) -> dict:
    """二值混淆 + Cohen's κ。pairs=(truth, pred)，pred∈{positive, negative}。"""
    n = len(pairs)
    if not n:
        return {"n": 0, "kappa": None, "accuracy": None}
    tp = sum(1 for t, j in pairs if t == "supported" and j == "positive")
    tn = sum(1 for t, j in pairs if t == "unsupported" and j == "negative")
    fp = sum(1 for t, j in pairs if t == "unsupported" and j == "positive")
    fn = sum(1 for t, j in pairs if t == "supported" and j == "negative")
    po = (tp + tn) / n
    p_pos = (tp + fn) / n
    p_j = (tp + fp) / n
    pe = p_pos * p_j + (1 - p_pos) * (1 - p_j)
    kappa = round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0
    return {"n": n, "kappa": kappa, "accuracy": round((tp + tn) / n, 3),
            "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn}}


def _kappa_raw(ps: list[tuple[str, str]]) -> float:
    """κ 裸值（bootstrap 重采样用，不四舍五入）。"""
    n = len(ps)
    tp = sum(1 for t, j in ps if t == "supported" and j == "positive")
    tn = sum(1 for t, j in ps if t == "unsupported" and j == "negative")
    fp = sum(1 for t, j in ps if t == "unsupported" and j == "positive")
    fn = sum(1 for t, j in ps if t == "supported" and j == "negative")
    po = (tp + tn) / n
    p_pos = (tp + fn) / n
    p_j = (tp + fp) / n
    pe = p_pos * p_j + (1 - p_pos) * (1 - p_j)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def _bootstrap_ci(pairs: list[tuple[str, str]], n_boot: int = 2000,
                  seed: int = 42) -> list[float] | None:
    """κ 95%CI（percentile bootstrap，固定种子可复现）。披露口径：报告必须带 n/CI，
    不能只有点估计。"""
    import random
    n = len(pairs)
    if n < 2:
        return None
    rng = random.Random(seed)
    boots = sorted(_kappa_raw([pairs[rng.randrange(n)] for _ in range(n)])
                   for _ in range(n_boot))
    return [round(boots[int(0.025 * n_boot)], 3), round(boots[int(0.975 * n_boot) - 1], 3)]


def main() -> int:
    pos = _load_positive_claims()
    print(f"正类样本: {len(pos)}")
    negs = _build_negatives(pos)
    print(f"负类样本: {len(negs)}")
    if len(pos) < 10 or len(negs) < 10:
        print("样本不足（检查 generative_trace）")
        return 1

    # eid_map：每个负类/正类样本独立构造（quote 所属证据文本）
    # 从正类 support_ref 取 chunk 文本不可得（trace 无全文）——按论文 DocIR
    # 归一反查：quote 归一包含于某块 → 该块文本即证据文本
    from app.rag.version_diff import load_docir_for
    d = json.loads(_GOLD_R3.read_text(encoding="utf-8"))
    pid = d.get("paper_id")
    docir = load_docir_for(pid) or {}
    blocks = docir.get("blocks", [])

    def _norm(t): return re.sub(r"\s+", "", str(t or "")).lower()
    block_index = [(b.get("text", ""), b.get("page", 0)) for b in blocks if b.get("text")]

    def evidence_for(quote: str) -> str:
        qn = _norm(quote)
        best, best_cov = "", 0.0
        for t, _pg in block_index:
            tn = _norm(t)
            if not tn:
                continue
            import difflib
            sm = difflib.SequenceMatcher(None, qn, tn, autojunk=False)
            matched = sum(x.size for x in sm.get_matching_blocks())
            cov = matched / max(len(qn), 1)
            if cov > best_cov:
                best_cov, best = cov, t
        return best if best_cov >= 0.7 else ""

    samples = []
    for i, c in enumerate(pos + negs, 1):
        # 错配负类：证据按「配对 claim」的 quote 反查（b 的块）——claim(=a) 与证据(=b)
        # 语义错位即负类本体；其余样本按自身 quote 反查
        eq = (c.get("support_ref") or {}).get("_evidence_quote") or c["quote"]
        ev = evidence_for(eq)
        truth = "unsupported" if "neg_kind" in c else "supported"
        samples.append({
            "id": f"s{i}", "text": c["text"], "quote": c["quote"],
            "evidence": ev, "truth": truth,
            "neg_kind": c.get("neg_kind", ""),
        })
    built = sum(1 for x in samples if x["evidence"])
    print(f"证据文本反查成功: {built}/{len(samples)}")

    usable = [s for s in samples if s["evidence"]]
    judge_pairs: list[tuple[str, str]] = []
    pipe_pairs: list[tuple[str, str]] = []
    per_batch: list[dict] = []
    for b0 in range(0, len(usable), _BATCH):
        chunk = usable[b0:b0 + _BATCH]
        eid_map = {k + 1: {"text": s["evidence"], "page": 0, "paper_id": pid,
                           "chunk_id": f"k{b0 + k}", "section_path": ""}
                   for k, s in enumerate(chunk)}

        def _mk(s, k, status, _b0=b0):   # 默认参绑定 b0（B023：闭包不共享循环变量）
            return {"text": s["text"], "status": status,
                    "evidence_ids": [k + 1], "quote": s["quote"],
                    "support": [{"eid": f"E{k + 1}", "page": 1, "quote": s["quote"],
                                 "chunk_id": f"k{_b0 + k}", "section_path": ""}]}

        # ---- 口径 A：judge-only（初始态=生产 L4 候选真实入口，verdict 落
        # semantic_status——判定读它而非 final status）----
        jc = [_mk(s, k, "supported") for k, s in enumerate(chunk)]
        from app.rag.verify import judge_claims as _judge
        judged, _g = _judge(jc, eid_map)
        for s, c in zip(chunk, judged):
            v = c.get("semantic_status")
            pred = {"yes": "positive", "partial": "positive",
                    "no": "negative"}.get(v, "unknown")
            s["judge_verdict"] = v or "unknown"
            if pred != "unknown":
                judge_pairs.append((s["truth"], pred))

        # ---- 口径 B：全管线（L2→L3→L4，final status 二值化）----
        pc = [_mk(s, k, "supported") for k, s in enumerate(chunk)]
        piped, _g2 = verify_claims(pc, eid_map)
        for s, c in zip(chunk, piped):
            st = c.get("status")
            pred = "positive" if st == "supported" else "negative"
            s["pipeline_status"] = st
            s["quote_status"] = c.get("quote_status")
            pipe_pairs.append((s["truth"], pred))
        per_batch.append({"n": len(chunk)})

    kj = _kappa(judge_pairs)
    kp = _kappa(pipe_pairs)
    ci_j = _bootstrap_ci(judge_pairs)
    ci_p = _bootstrap_ci(pipe_pairs)
    import time as _time
    out = {
        "time": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_evaluated": kj["n"], "n_pipeline": kp["n"],
        "cohens_kappa": kj["kappa"], "accuracy": kj["accuracy"],
        "confusion": kj.get("confusion"),
        "kappa_ci95": ci_j,
        "pipeline_kappa": kp["kappa"], "pipeline_accuracy": kp["accuracy"],
        "pipeline_confusion": kp.get("confusion"),
        "pipeline_kappa_ci95": ci_p,
        "kappa_note": "构造真值口径：真实 supported + 程序负类（数值/实体/错配）；"
                      "judge-only κ 读 semantic_status，pipeline κ 读 final status。"
                      "正类来自管线自身产物，非独立人工标注——负类为构造校准口径",
        "ci_note": f"95%CI bootstrap 2000 次重采样（固定种子），n={kj['n']}",
        "batches": len(per_batch),
        "samples": samples,
    }
    dest = Path(__file__).parent / "kappa_report.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[判断器 L4] κ = {kj['kappa']} | acc = {kj['accuracy']} | n = {kj['n']} "
          f"| 混淆 {kj.get('confusion')}")
    print(f"[全管线]   κ = {kp['kappa']} | acc = {kp['accuracy']} | n = {kp['n']} "
          f"| 混淆 {kp.get('confusion')}")
    print(f"目标 κ≥0.60：judge={kj['kappa'] and kj['kappa'] >= 0.60} "
          f"pipeline={kp['kappa'] and kp['kappa'] >= 0.60}")
    print(f"已写出 {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
