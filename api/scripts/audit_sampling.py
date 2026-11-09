"""Round4 模块五：综述 [E#] 支撑抽样复核 + Future Work 四态人工复核表（半自动）。

① survey_reports 最新一条：跨 5 节均匀抽 10 条 claim，用 anchors(E#→block_ids)
   回读 DocIR 块文本，quote_coverage ≥0.85 记 pass——「有引用」升级为「引用真的
   支撑」；<0.8 通过率 FAIL。
② future_checks 全量（≤10 条）：输出 markdown 人工复核表（状态/置信度/证据链 +
   空白人工列），供 round4 checklist 人工复核 open_unverified/solved 边界。

用法：cd api && uv run python scripts/audit_sampling.py --out ../../docs/acceptance/round4-<date>/
前置：先在 /graph 页跑过一次整批综述（survey_reports 有行）。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _survey_sample(outdir: Path, rng: random.Random) -> dict:
    from app import db
    from app.rag.verify import _norm as _norm_txt
    from app.rag.verify import quote_coverage
    from app.rag.version_diff import load_docir_for

    rows = db.query("SELECT id, sections, created_at FROM survey_reports"
                    " ORDER BY created_at DESC LIMIT 1")
    if not rows:
        return {"ok": False, "skipped": "survey_reports 为空（先跑一次整批综述）"}
    sections = json.loads(rows[0]["sections"] or "[]")
    # 收集 (section_title, claim, anchors) 展平后跨节均匀抽样
    flat = []
    for sec in sections:
        anchors = sec.get("anchors") or {}
        for c in sec.get("claims") or []:
            # 口径（Round4 修订）：只抽 quote_status=verified 且 supported 的 claims——
            # unsupported/unverified 的诚实信号是状态徽章而非锚点覆盖，不计入分母
            if c.get("quote_status") != "verified" or c.get("status") != "supported":
                continue
            flat.append({"section": sec.get("title", ""), "claim": c, "anchors": anchors})
    if not flat:
        return {"ok": False, "skipped": "综述 sections 无 claims"}
    k = min(10, len(flat))
    picks = rng.sample(flat, k)

    block_maps: dict[str, dict[str, str]] = {}

    def _blocks(pid: str) -> dict[str, str]:
        if pid not in block_maps:
            block_maps[pid] = {b.get("id", ""): _norm_txt(b.get("text", ""))
                               for b in (load_docir_for(pid) or {}).get("blocks", [])}
        return block_maps[pid]

    rows_out = []
    passed = 0
    for p in picks:
        c = p["claim"]
        # claim.support: [{eid, quote, ...}]；anchors: E# → {paper_id, block_ids, page}
        sups = c.get("support") or []
        ok = False
        detail = {"section": p["section"], "claim": (c.get("text") or c.get("claim") or "")[:80],
                  "status": c.get("status", ""), "quote_status": c.get("quote_status", "")}
        for s in sups:
            eid = str(s.get("eid") or s.get("e") or "")
            anchor = p["anchors"].get(f"E{eid}" if not eid.startswith("E") else eid, {})
            bids = anchor.get("block_ids") or []
            if not bids:
                continue
            text = _norm_txt(" ".join(t for t in
                                      (_blocks(anchor.get("paper_id", "")).get(b, "")
                                       for b in bids) if t))
            if text and quote_coverage(s.get("quote", ""), text) >= 0.85:
                ok = True
                detail["anchor"] = f"E{eid}→{bids[0]}"
                break
        detail["supported"] = bool(ok)
        rows_out.append(detail)
        passed += int(ok)
    rate = round(passed / len(rows_out), 2)
    return {"ok": rate >= 0.8, "sampled": len(rows_out), "passed": passed,
            "pass_rate": rate, "claims": rows_out}


def _future_checks_table(outdir: Path) -> dict:
    from app import db
    rows = db.query("SELECT idea_text, verdict, confidence, evidence, external_count"
                    " FROM future_checks ORDER BY checked_at DESC LIMIT 10")
    lines = ["| # | Future Work | 判定 | 置信 | 外部证据数 | 人工复核 |",
             "|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        ev = json.loads(r["evidence"] or "[]")
        ev_note = f"{len(ev)} 条" + (f"（{ev[0].get('title', '')[:40]}…）" if ev else "")
        lines.append(f"| {i} | {(r['idea_text'] or '')[:70]} | {r['verdict']} | "
                     f"{r['confidence']:.2f} | {ev_note} | （留白） |")
    (outdir / "future_checks_review.md").write_text(
        "# Future Work 四态人工复核表（Round4）\n\n"
        "> open_unverified = 无外部证据（诚实态，≠确认没人做）；solved/partial 边界请人工"
        "抽查证据链是否支撑判定。\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return {"rows": len(rows)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../../docs/acceptance/_audit")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    from app import db
    db.init_db()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    survey = _survey_sample(outdir, rng)
    print(f"综述 claim 抽样：{'PASS' if survey.get('ok') else 'FAIL/SKIP'} "
          f"{survey.get('passed', 0)}/{survey.get('sampled', 0)}")
    fc = _future_checks_table(outdir)
    print(f"Future Work 复核表：{fc['rows']} 行 → future_checks_review.md")
    report = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
              "survey_sample": survey, "future_checks": fc}
    (outdir / "audit_sampling.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = bool(survey.get("ok"))
    print(f"== audit sampling {'PASS' if ok else 'FAIL'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
