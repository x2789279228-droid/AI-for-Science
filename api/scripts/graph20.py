"""Round4 模块五：≥18 篇全量建图真实记录（graph20.json）——节点/边/角色分布/
外部引用边/构建耗时 + 阈值校准检查（基石/边缘非全空非全基石）。

用法：cd api && uv run python scripts/graph20.py --out ../../docs/acceptance/round4-<date>/
前置：库内 ≥18 篇已解析（scripts/fetch_corpus.py --snapshot）；宿主机 .env。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _run_survey(client, paper_ids: list[str]) -> dict:
    """整批综述（SSE 拉到 final；路由自动写 survey_reports 供 audit_sampling 抽检）。"""
    import json as _json

    t0 = time.time()
    final = None
    with client.stream("POST", "/api/graph/survey", json={"paper_ids": paper_ids}) as r:
        assert r.status_code == 200, r.text
        event = ""
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: ") and event == "final":
                final = _json.loads(line[6:])
    elapsed = round(time.time() - t0, 1)
    sections = (final or {}).get("sections", [])
    claims = sum(len(s.get("claims") or []) for s in sections)
    return {"elapsed_s": elapsed, "sections": len(sections), "claims": claims,
            "ok": bool(sections)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../../docs/acceptance/_graph20")
    ap.add_argument("--min-papers", type=int, default=18)
    ap.add_argument("--with-survey", action="store_true",
                    help="建图后跑一次整批综述（写 survey_reports，供 audit_sampling 抽检）")
    args = ap.parse_args()
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app
    db.init_db()
    client = TestClient(app)

    rows = db.query("SELECT id, title FROM papers WHERE status='active' AND docir_path != ''")
    n = len(rows)
    if n < args.min_papers:
        print(f"库内仅 {n} 篇已解析（需 ≥{args.min_papers}）：先跑 scripts/fetch_corpus.py --snapshot")
        return 1
    print(f"建图对象：{n} 篇")
    t0 = time.time()
    r = client.get("/api/graph")
    elapsed = round(time.time() - t0, 1)
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:200]}")
        return 1
    g = r.json()
    nodes = g.get("nodes", [])
    roles = {"cornerstone": sum(1 for x in nodes if x.get("role") == "cornerstone"),
             "peripheral": sum(1 for x in nodes if x.get("role") == "peripheral"),
             "": sum(1 for x in nodes if not x.get("role"))}
    survey = _run_survey(client, [x["paper_id"] for x in nodes]) if args.with_survey else None
    if survey:
        print(f"综述：{survey['sections']} 节 · {survey['claims']} claims · {survey['elapsed_s']}s")
    report = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "paper_count": n,
        "elapsed_s": elapsed,
        "node_count": len(nodes),
        "edge_count": g.get("edge_count"),
        "external_count": g.get("external_count"),
        "communities": [{"id": c.get("id"), "size": c.get("size")} for c in g.get("communities", [])],
        "role_counts": roles,
        "class_stat": g.get("class_stat"),
        "survey": survey,
        "papers": [{"id": x["paper_id"], "title": (x.get("title") or "")[:40],
                    "role": x.get("role", ""), "in_degree": x.get("in_degree"),
                    "pagerank": x.get("pagerank")} for x in nodes],
    }
    # 阈值校准：非全空、非全基石/边缘（20 篇下 P70/P30 判据应产出分化而非饱和）
    calibrated = (roles["cornerstone"] > 0 and roles["cornerstone"] < n
                  and roles[""] < n)
    report["threshold_calibrated"] = bool(calibrated)
    ok = bool(len(nodes) >= args.min_papers and (g.get("edge_count") or 0) > 0 and calibrated
              and (survey is None or survey.get("ok")))
    report["ok"] = bool(ok)   # 门禁值入档（summary/自评表直接引用）
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "graph20.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    print(f"nodes={len(nodes)} edges={g.get('edge_count')} external={g.get('external_count')} "
          f"roles={roles} · {elapsed}s")
    print(f"== graph20 {'PASS' if ok else 'FAIL'} ==")
    print(f"已写出：{outdir / 'graph20.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
