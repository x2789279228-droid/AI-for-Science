#!/usr/bin/env python
"""生成唯一事实源：web/lib/evidence.ts + docs/acceptance/latest-verify/summary.md。

用法（在 api 环境或系统 Python 均可，仅用标准库）：
  python scripts/write_evidence.py \
    --pytest-log docs/acceptance/latest-verify/pytest.log \
    --e2e-log docs/acceptance/latest-verify/playwright.log \
    --eval-json docs/acceptance/eval-20261114-r3/eval_gold.json \
    --db data-real/scholar.db

设计目的：README / /judge / /health 的数字不再手写——先跑门禁把日志落到
docs/acceptance/latest-verify/，再由本脚本统一生成页面与摘要，杜绝口径漂移。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # scholar-agent/（api/scripts/ → 仓库根）


def _read(path: Path) -> str:
    """读日志：兼容 PowerShell Tee-Object 的 UTF-16LE 与常规 UTF-8。"""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_pytest(text: str) -> dict:
    def _n(pat: str) -> int:
        m = re.search(pat, text)
        return int(m.group(1)) if m else 0

    passed = _n(r"(\d+)\s+passed")
    failed = _n(r"(\d+)\s+failed")
    errors = _n(r"(\d+)\s+error")
    skipped = _n(r"(\d+)\s+skipped")
    return {"passed": passed, "failed": failed, "errors": errors, "skipped": skipped,
            "total": passed + failed + errors + skipped}


def parse_playwright(text: str) -> dict:
    def _n(pat: str) -> int:
        m = re.search(pat, text)
        return int(m.group(1)) if m else 0

    passed = _n(r"(\d+)\s+passed")
    failed = _n(r"(\d+)\s+failed")
    skipped = _n(r"(\d+)\s+skipped")
    return {"passed": passed, "failed": failed, "skipped": skipped,
            "total": passed + failed + skipped}


def parse_eval(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {"round": "", "gatesPassed": 0, "gatesTotal": 0, "compareOk": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"round": "", "gatesPassed": 0, "gatesTotal": 0, "compareOk": None}
    gates = data.get("gates") or {}
    passed = sum(1 for v in gates.values() if v)
    round_name = path.parent.name
    return {"round": round_name, "gatesPassed": passed, "gatesTotal": len(gates),
            "compareOk": bool(gates.get("compare_ok")) if "compare_ok" in gates else None}


def parse_corpus(db_path: Path) -> dict:
    papers = refs = 0
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            papers = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE status='active'").fetchone()[0]
            refs = conn.execute('SELECT COUNT(*) FROM "references"').fetchone()[0]
            conn.close()
        except sqlite3.Error:
            pass
    return {"papers": papers, "references": refs, "source": str(db_path.name)}


def git_commit() -> tuple[str, str]:
    def _run(args: list[str]) -> str:
        try:
            return subprocess.check_output(args, cwd=ROOT, text=True,
                                           stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return ""

    return _run(["git", "rev-parse", "--short", "HEAD"]), _run(["git", "branch", "--show-current"])


def find_latest_eval() -> Path | None:
    candidates = sorted(ROOT.glob("docs/acceptance/eval-*/**/eval_gold.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def render_ts(ev: dict) -> str:
    def q(s: object) -> str:
        return json.dumps(s, ensure_ascii=False)

    return (
        "// 自动生成：scripts/write_evidence.py —— 请勿手改。\n"
        "// README / /judge / /health 的数字唯一来源；更新方式见文件头说明。\n"
        "export const EVIDENCE = {\n"
        f"  generatedAt: {q(ev['generatedAt'])},\n"
        f"  commit: {q(ev['commit'])},\n"
        f"  branch: {q(ev['branch'])},\n"
        f"  pytest: {{ passed: {ev['pytest']['passed']}, failed: {ev['pytest']['failed']}, "
        f"errors: {ev['pytest']['errors']}, skipped: {ev['pytest']['skipped']}, "
        f"total: {ev['pytest']['total']} }},\n"
        f"  e2e: {{ passed: {ev['e2e']['passed']}, failed: {ev['e2e']['failed']}, "
        f"skipped: {ev['e2e']['skipped']}, total: {ev['e2e']['total']} }},\n"
        f"  eval: {{ round: {q(ev['eval']['round'])}, gatesPassed: {ev['eval']['gatesPassed']}, "
        f"gatesTotal: {ev['eval']['gatesTotal']}, compareOk: {json.dumps(ev['eval']['compareOk'])} }},\n"
        f"  corpus: {{ papers: {ev['corpus']['papers']}, references: {ev['corpus']['references']}, "
        f"source: {q(ev['corpus']['source'])} }},\n"
        "} as const;\n\n"
        "export type Evidence = typeof EVIDENCE;\n"
    )


def render_summary(ev: dict) -> str:
    p, e, evl, c = ev["pytest"], ev["e2e"], ev["eval"], ev["corpus"]
    return (
        "# 最近一次可复跑证据（自动生成）\n\n"
        f"- 生成时间：{ev['generatedAt']}\n"
        f"- 代码基线：`{ev['branch']}` @ `{ev['commit']}`\n\n"
        "| 门禁 | 结果 |\n|---|---|\n"
        f"| pytest | {p['passed']} passed / {p['failed']} failed / {p['errors']} errors "
        f"（total {p['total']}） |\n"
        f"| Playwright e2e | {e['passed']} passed / {e['failed']} failed / {e['skipped']} skipped "
        f"（total {e['total']}） |\n"
        f"| 诚实评测 eval | {evl['gatesPassed']}/{evl['gatesTotal']} gates"
        f"（compare_ok={evl['compareOk']}；round {evl['round'] or 'n/a'}） |\n"
        f"| 演示语料 | {c['papers']} 篇 active / {c['references']} 条 references（{c['source']}） |\n\n"
        "> 页面数字由本文件同源生成；禁止在 README / 页面手写测试数字。\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pytest-log", default="docs/acceptance/latest-verify/pytest.log")
    ap.add_argument("--e2e-log", default="docs/acceptance/latest-verify/playwright.log")
    ap.add_argument("--eval-json", default="")
    ap.add_argument("--db", default="data-real/scholar.db")
    ap.add_argument("--out", default="web/lib/evidence.ts")
    ap.add_argument("--summary", default="docs/acceptance/latest-verify/summary.md")
    args = ap.parse_args()

    pytest_log = ROOT / args.pytest_log
    e2e_log = ROOT / args.e2e_log
    eval_path = ROOT / args.eval_json if args.eval_json else find_latest_eval()

    commit, branch = git_commit()
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M +08")
    ev = {
        "generatedAt": now,
        "commit": commit or "unknown",
        "branch": branch or "unknown",
        "pytest": parse_pytest(_read(pytest_log)),
        "e2e": parse_playwright(_read(e2e_log)),
        "eval": parse_eval(eval_path),
        "corpus": parse_corpus(ROOT / args.db),
    }
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_ts(ev), encoding="utf-8")
    summary = ROOT / args.summary
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(render_summary(ev), encoding="utf-8")
    print(f"[evidence] -> {out}")
    print(f"[evidence] -> {summary}")
    print(json.dumps(ev, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
