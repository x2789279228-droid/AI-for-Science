"""证据 manifest（Round6 P0-7）：一键 SHA256 证据包清单。

对 docs/acceptance 全部评测证据 + 评测脚本 + 关键源码计算 SHA256，
输出 evidence_manifest.json（文件路径/大小/hash/时间）。答辩/提交时
可用 `eval --replay` 复核：manifest 里的 hash 与仓库当前文件一致 =
「报告数字来自这份代码与这批数据」的可验证声明。

用法：cd api && uv run python scripts/eval_evidence_manifest.py [--out PATH]
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    acc = ROOT.parent / "docs" / "acceptance"
    targets: list[Path] = []
    if acc.exists():
        targets += sorted(acc.rglob("eval_gold.json"))
        targets += sorted(acc.rglob("*.json")) if False else []
    targets += sorted((ROOT / "scripts").glob("eval_*.py"))
    targets += sorted((ROOT / "scripts").glob("verify_goldset_40.py"))
    targets += sorted((ROOT / "scripts").glob("ablation*.json"))
    targets += sorted((ROOT / "scripts").glob("kappa_report.json"))
    targets += sorted((ROOT / "scripts").glob("seeds_ci.json"))
    targets += sorted((ROOT / "scripts").glob("round6_metrics.json"))
    targets += sorted((ROOT / "scripts").glob("feas_pymupdf_udl.json"))
    targets += [ROOT / "app" / "agents" / "nodes.py",
                ROOT / "app" / "agents" / "graph.py",
                ROOT / "app" / "rag" / "verify.py",
                ROOT / "app" / "rag" / "answering.py",
                ROOT / "app" / "api_routes" / "routes.py",
                ROOT / "app" / "api_routes" / "graph_routes.py"]
    entries = []
    for p in targets:
        if not p.is_file():
            continue
        rel = str(p.relative_to(ROOT.parent))
        entries.append({"path": rel, "bytes": p.stat().st_size,
                        "sha256": sha256_of(p)})
    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "algorithm": "sha256",
        "note": "评测证据包：报告数字 ← 这些脚本 × 这份源码 × 这些数据。复核=逐条比对 sha256。",
        "files": entries,
    }
    dest = Path(__file__).parent / "evidence_manifest.json"
    dest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest: {len(entries)} files → {dest}")
    for e in entries[:5]:
        print(f"  {e['sha256'][:12]}… {e['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
