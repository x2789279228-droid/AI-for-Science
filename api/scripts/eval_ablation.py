"""消融实验 + naive 基线横评（Round6，评分表「架构不是摆设」硬证据）。

配置（进程内 monkeypatch，闭密环境、EMBEDDING off 走 BM25-only 时如实在列）：
  full           — 系统全量
  no-verifier    — Verifier 恒通过（证据充分性校验/重检索环去掉）
  no-anchor      — refine_support_blocks 关（块级锚点精化去掉，退 chunk 级）
  no-dense       — dense_search → []（纯 BM25 单通道）
  no-rerank      — rerank 后端 off（RRF 原始序）
  naive-baseline — 横评基线：BM25 top5 直出抽取式（无 Verifier/锚点/引用后验/claims）

指标：Recall@5 / MRR@5（10 题金标检索，确定性）；生成类（全 LLM 配置）另测
supported_ratio / hallucination（eval_gold 同口径，少量题演示掉分方向）。
Wilcoxon 符号秩 p 值：手写正态近似（无 scipy 依赖）。

用法：cd api && uv run python scripts/eval_ablation.py [--generative]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_DATA = Path(__file__).parent.parent / "tests" / f"_abl_{int(time.time())}"
os.environ.setdefault("SCHOLAR_DATA_DIR", str(_DATA))
os.environ.setdefault("MINERU_ENABLED", "off")
os.environ.setdefault("RATE_LIMIT_PER_MIN", "0")
# 注意：EMBEDDING/LLM 凭据**不屏蔽**——消融的 dense/rerank/生成类掉分
# 只在真引擎下有信息量（BM25-only 环境里 no-dense/no-rerank 与 full 恒等）。

from app import db
from app.main import app  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

# 金标检索题（与 eval_gold 同源，10 题）
_EN_FACTS = [
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


def _pick_paper() -> str:
    rows = db.query("SELECT id, title FROM papers WHERE docir_path IS NOT NULL AND docir_path != '' "
                    "AND COALESCE(doc_type,'paper')='paper' ORDER BY created_at DESC")
    for r in rows:
        t = (r["title"] or "").lower()
        if "attention is all you need" in t:
            return r["id"]
    return rows[0]["id"] if rows else ""


def _ensure_paper(client: TestClient) -> str:
    pid = _pick_paper()
    if pid:
        return pid
    import io
    pdf = Path(__file__).resolve().parents[3] / "downloads" / "1706.03762" / "1706.03762v7.pdf"
    r = client.post("/api/papers", files={"file": ("v7.pdf", io.BytesIO(pdf.read_bytes()),
                                                  "application/pdf")}, data={"doc_type": "paper"})
    job = r.json().get("job_id")
    while job:
        j = client.get(f"/api/jobs/{job}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            break
        time.sleep(0.3)
    return _pick_paper()


# ---------- 检索指标（确定性，不依赖 LLM） ----------

def _load_qset() -> list[tuple[str, tuple[str, ...]]]:
    """40 题金标集（verify_goldset_40.py 验证收录版）优先；缺失回退内置 10 题。"""
    f = Path(__file__).parent / "goldset_47q.json"
    if f.exists():
        d = json.loads(f.read_text(encoding="utf-8"))
        qs = [(q, tuple(g) if isinstance(g, list) else (g,)) for q, g in d.get("questions", [])]
        if len(qs) >= 20:
            return qs
    return _EN_FACTS


def recall_mrr(indexing, pid: str) -> dict:
    hits5, rrs = [], []
    for q, golds in _load_qset():
        res = indexing.hybrid_search([pid], q, top_k=5)
        hs = res.get("hits", [])
        ok = any(g.lower() in (h.get("text") or "").lower() for g in golds for h in hs)
        rank = next((i + 1 for i, h in enumerate(hs)
                     if any(g.lower() in (h.get("text") or "").lower() for g in golds)), 0)
        hits5.append(ok)
        rrs.append(1.0 / rank if rank else 0.0)
    return {"recall_at5": round(sum(hits5) / len(hits5), 2),
            "mrr_at5": round(sum(rrs) / len(rrs), 2)}


# ---------- 生成指标（LLM，可选） ----------

def generative_metrics(client: TestClient, pid: str, n: int = 5) -> dict:
    from app.rag.verify import verify_claims
    qs = ["How many GPUs were the base models trained on?",
          "What BLEU score did the big model achieve on EN-DE?",
          "How long did the big model train for?",
          "Which optimizer was used?",
          "What is the model dimension d_model?"][:n]
    supported = claims_n = 0
    for q in qs:
        conv = f"abl_{time.time_ns()}"
        final = None
        event = ""
        buf = b""
        with client.stream("POST", "/api/chat", json={
                "question": q, "paper_ids": [pid], "conversation_id": conv,
                "agent": "reader", "doc_mode": "paper"}) as r:
            for chunk in r.iter_bytes():
                buf += chunk
                sep = b"\n\n"
                while sep in buf:
                    frame, buf = buf.split(sep, 1)
                    event = ""
                    for line in frame.decode("utf-8", "replace").splitlines():
                        if line.startswith("event: "):
                            event = line[7:].strip()
                        elif line.startswith("data: ") and event == "final":
                            final = json.loads(line[6:])
                    if event == "done":
                        break
                if event == "done":
                    break
        cl = final.get("claims") or []
        claims_n += len(cl)
        supported += sum(1 for c in cl if c.get("status") == "supported")
    sup = round(supported / claims_n, 2) if claims_n else None
    return {"supported_ratio": sup, "claims": claims_n,
            "hallucination_rate": round(1 - sup, 2) if sup is not None else None}


# ---------- Wilcoxon 符号秩（正态近似，无 scipy 依赖） ----------

def wilcoxon_p(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 1.0
    diffs = [x - y for x, y in zip(a, b) if x != y]
    if not diffs:
        return 1.0
    ranked = sorted(abs(d) for d in diffs)
    w = sum(r for r, d in zip(ranked, sorted(diffs, key=abs)) if d > 0)
    n = len(diffs)
    mu = n * (n + 1) / 4
    sigma = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
    if sigma == 0:
        return 1.0
    z = (w - mu) / sigma
    return round(2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))), 4)


# ---------- 配置上下文（monkeypatch 上下文管理器） ----------

class _Patch:
    def __init__(self, patches: list[tuple]):
        self.patches = patches   # [(target_obj, attr_name, replacement_or_MISSING)]
        self.applied: list[tuple] = []

    def __enter__(self):
        import app.rag.answering as answering
        import app.agents.nodes as nodes
        for obj, attr, repl in self.patches:
            old = getattr(obj, attr)
            self.applied.append((obj, attr, old))
            setattr(obj, attr, repl)
        return self

    def __exit__(self, *exc):
        for obj, attr, old in reversed(self.applied):
            setattr(obj, attr, old)
        self.applied.clear()


MISSING = object()


def run_config(name: str, client: TestClient, pid: str, do_generative: bool) -> dict:
    from app.rag import indexing
    from app.rag import answering
    from app.agents import nodes as nodes_mod

    def _empty(*a, **k):
        return []

    def _zero(*a, **k):
        return 0

    def _always_pass(state):
        return True

    ctx = {
        "full": [],
        "no-verifier": [(nodes_mod, "_check_sufficiency", _always_pass)],
        "no-anchor": [(answering, "refine_support_blocks", _zero)],
        "no-dense": [(indexing, "dense_search", _empty)],
        "no-rerank": [(indexing, "rerank_hits", _empty)],
        "naive-baseline": [(indexing, "dense_search", _empty),
                           (indexing, "rerank_hits", _empty),
                           (nodes_mod, "_check_sufficiency", _always_pass),
                           (answering, "refine_support_blocks", _zero)],
    }[name]
    out: dict = {"config": name}
    with _Patch(ctx):
        out.update(recall_mrr(indexing, pid))
        if do_generative:
            out.update(generative_metrics(client, pid))
    print(f"  [{name:>14}] {out}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generative", action="store_true", help="加跑生成类指标（需真实 LLM）")
    ap.add_argument("--out", default=str(Path(__file__).parent / "ablation.json"))
    args = ap.parse_args()

    db.init_db()
    client = TestClient(app)
    pid = _ensure_paper(client)
    assert pid, "无可用论文"
    print(f"paper={pid} configs=6")

    configs = ["full", "no-verifier", "no-anchor", "no-dense", "no-rerank", "naive-baseline"]
    results = {c: run_config(c, client, pid, args.generative) for c in configs}

    # Wilcoxon：full vs 各消融（逐题 recall 差值方向性检验）
    per_q = {c: [] for c in configs}
    from app.rag import indexing
    _QSET = _load_qset()
    for q, golds in _QSET:
        for c in configs:
            ctx = {
                "full": [], "no-verifier": [], "no-anchor": [],
                "no-dense": [(indexing, "dense_search", lambda *a, **k: [])],
                "no-rerank": [(indexing, "rerank_hits", lambda *a, **k: [])],
                "naive-baseline": [(indexing, "dense_search", lambda *a, **k: []),
                                   (indexing, "rerank_hits", lambda *a, **k: [])],
            }[c]
            with _Patch(ctx):
                res = indexing.hybrid_search([pid], q, top_k=5)
                hs = res.get("hits", [])
                rank = next((i + 1 for i, h in enumerate(hs)
                             if any(g.lower() in (h.get("text") or "").lower()
                                    for g in golds)), 0)
                per_q[c].append(1.0 / rank if rank else 0.0)   # 逐题 RR（连续值，功效高）
    stats = {}
    for c in configs[1:]:
        p = wilcoxon_p(per_q["full"], per_q[c])
        stats[f"full_vs_{c}"] = {
            "p_value": p,
            "full_mrr": per_q["full"], f"{c}_mrr": per_q[c],
            "significant_p<0.05": p < 0.05,
        }
    report = {"paper_id": pid, "configs": results, "wilcoxon": stats,
              "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
