"""Round4 性能基准（bench.json）：上限/降级可量化。

口径：**确定性优先**——脚本内强制 LLM/Embedding 关闭（子环境变量），测的 是
检索/规则/渲染链路的可复现性能；宿主真实 LLM 的端到端时延已在 eval_gold
（generative elapsed_s）与 qa_logs（avg_latency_ms）有据，不重复。

① graph_build：全库建图（classify_citations 规则降级 + Louvain + 角色）耗时
② csv_100k：10 万行真实生成 CSV 的 parse→store→序列化耗时（行预算截断路径）
③ chat_concurrent：10 并发 reader 问答（LLM off 抽取式降级）p50/p95/错误数
   ——同时验证降级链路在并发下不炸
④ pdf_overlay：20+ 页论文 overlay 导出耗时（无缓存译文则 SKIP 如实标注）

用法：cd api && uv run python scripts/bench.py --out ../../docs/acceptance/round4-<date>/
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 确定性口径：在导入 app 之前关闭外部依赖（LLM/Embedding）
os.environ["LLM_API_KEY"] = ""
os.environ["EMBEDDING_BACKEND"] = "off"
os.environ["MINERU_ENABLED"] = "off"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def bench_graph() -> dict:
    from app import db
    from app.agents.insights import enrich
    from app.agents.tools import _citation_graph

    rows = db.query("SELECT id FROM papers WHERE status='active' AND docir_path != ''")
    ids = [r["id"] for r in rows]
    if not ids:
        return {"skipped": "库内无已解析论文"}
    t0 = time.time()
    g = enrich(_citation_graph(ids))
    return {"papers": len(ids), "nodes": len(g.get("nodes", [])),
            "edges": g.get("edge_count", 0), "elapsed_s": round(time.time() - t0, 1)}


def bench_csv_100k() -> dict:
    from app.agents import csv_viz

    rows = ["epoch,train_loss,val_loss"] + \
        [f"{i},{0.5 * pow(0.99, i):.4f},{0.52 * pow(0.99, i):.4f}" for i in range(100_000)]
    raw = "\n".join(rows).encode()
    t0 = time.time()
    parsed = csv_viz.parse_csv(raw, "bench100k.csv")
    t_parse = time.time() - t0
    t0 = time.time()
    js, kept = csv_viz._serialize_rows_bounded(parsed["rows"], parsed["columns"])
    t_store = time.time() - t0
    return {"rows": parsed["row_count"], "truncated": parsed["truncated"],
            "parse_s": round(t_parse, 2), "serialize_s": round(t_store, 2),
            "serialized_rows": kept, "json_valid": json.loads(js) is not None}


def _chat_once(client, paper_id: str, i: int) -> tuple[float, int]:
    t0 = time.time()
    try:
        with client.stream("POST", "/api/chat",
                           json={"question": "What is the model dimension d_model?",
                                 "paper_ids": [paper_id], "agent": "reader",
                                 "conversation_id": f"bench_{i}"}) as r:
            code = r.status_code
            for _ in r.iter_lines():
                pass
        return time.time() - t0, code
    except Exception:  # noqa: BLE001 —— 错误计数即目的
        return time.time() - t0, 599


def bench_chat_concurrent(n: int = 10) -> dict:
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app

    rows = db.query("SELECT id FROM papers WHERE status='active' AND docir_path != '' LIMIT 1")
    if not rows:
        return {"skipped": "库内无已解析论文"}
    pid = rows[0]["id"]
    out: list[tuple[float, int]] = []
    lock = threading.Lock()

    def _run(i: int) -> None:
        c = TestClient(app)   # 线程独立 client（进程内 ASGI，无端口竞争）
        r = _chat_once(c, pid, i)
        with lock:
            out.append(r)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(_run, range(n)))
    wall = time.time() - t0
    codes = [c for _, c in out]
    lats = sorted(l for l, _ in out)
    return {"concurrency": n, "wall_s": round(wall, 1),
            "p50_ms": round(statistics.median(lats) * 1000, 0),
            "p95_ms": round(lats[int(len(lats) * 0.95) - 1] * 1000, 0) if lats else 0,
            "errors": sum(1 for c in codes if c >= 500),
            "ok": all(c == 200 for c in codes)}


def bench_pdf_overlay() -> dict:
    import fitz
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app

    rows = db.query("SELECT id, page_count FROM papers WHERE status='active' AND docir_path != ''"
                    " AND page_count >= 20")
    if not rows:
        return {"skipped": "库内无 ≥20 页论文"}
    pid = rows[0]["id"]
    client = TestClient(app)
    cached = client.get(f"/api/papers/{pid}/translation?lang=zh").json().get("blocks", {})
    if not cached:
        return {"skipped": "该论文无缓存译文（先翻 2-3 页再跑；如实跳过）"}
    t0 = time.time()
    r = client.get(f"/api/papers/{pid}/export?fmt=pdf&target_lang=zh&mode=academic&layout=overlay")
    elapsed = round(time.time() - t0, 1)
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}"}
    doc = fitz.open(stream=r.content, filetype="pdf")
    out = {"paper_id": pid, "pages": doc.page_count, "overflow": int(r.headers.get("X-Export-Overflow") or 0),
           "elapsed_s": elapsed}
    doc.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../../docs/acceptance/_bench")
    args = ap.parse_args()
    from app import db
    db.init_db()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    print("== ① graph build（LLM off 确定性）==")
    graph = bench_graph()
    print(" ", graph)
    print("== ② CSV 100k == ")
    csv_b = bench_csv_100k()
    print(" ", csv_b)
    print("== ③ 10 并发问答（LLM off 抽取式降级）==")
    chat = bench_chat_concurrent(10)
    print(" ", chat)
    print("== ④ PDF overlay（20+ 页，需缓存译文）==")
    pdf = bench_pdf_overlay()
    print(" ", pdf)

    report = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "deterministic(LLM off)",
              "graph_build": graph, "csv_100k": csv_b, "chat_concurrent": chat,
              "pdf_overlay": pdf,
              "ok": bool(graph.get("nodes") and csv_b.get("json_valid")
                         and chat.get("ok") and not pdf.get("error"))}
    (outdir / "bench.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print(f"\n== bench {'PASS' if report['ok'] else 'FAIL'} ==")
    print(f"已写出：{outdir / 'bench.json'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
