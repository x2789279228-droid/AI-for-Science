"""Round6：同标题多副本收敛（工作库 + data-real 快照库）。

语料多轮抓取留下同标题副本（Attention×3/BERT×3…），块结构与 refs 数不一，
金标选择器（created_at 排序 + 前缀匹配）命中的副本存在随机性——收敛为
「每标题保留最健康一份，其余软归档（status='archived'，查询/图谱/推荐即排除）」。

健康度 = (references 条数, chunks 有向量数) 元组序。快照库同步归档并清理
孤儿 docir 文件（无 active paper 引用者）。

用法：cd api && uv run python scripts/converge_duplicates.py [--apply]
默认 dry-run 打印计划；--apply 执行。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def converge(db_path: Path, *, apply: bool, docir_dir: Path | None) -> None:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    rows = conn.execute(
        "SELECT id, title, status, docir_path FROM papers WHERE docir_path != '' "
        "AND docir_path IS NOT NULL").fetchall()
    groups: dict[str, list[tuple]] = {}
    for pid, title, status, dp in rows:
        groups.setdefault(_norm_title(title), []).append((pid, title, status, dp))

    plan: list[tuple[str, str, str]] = []   # (keep, archive, reason)
    for title, items in sorted(groups.items()):
        if len(items) < 2:
            continue
        scored = []
        for pid, t, status, dp in items:
            refs = conn.execute('SELECT COUNT(*) FROM "references" WHERE paper_id=?',
                                (pid,)).fetchone()[0]
            vec = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE paper_id=? AND vec IS NOT NULL",
                (pid,)).fetchone()[0]
            scored.append(((refs, vec), pid, t, status))
        scored.sort(reverse=True)
        keep = scored[0]
        # 并列时保 active（eval/compare 的 created_at 序选择在并列健康度下无差别）
        for _, pid, t, status in scored[1:]:
            reason = f"keep={keep[1]}(refs {keep[0][0]},vec {keep[0][1]}) > {pid}(refs/vect 次优)"
            plan.append((keep[1], pid, reason))
    print(f"{db_path}: {len(plan)} 个副本待归档")
    for keep, arc, reason in plan:
        print(f"  archive {arc} ← {reason}")
    if not apply:
        conn.close()
        return
    for keep, arc, _ in plan:
        conn.execute("UPDATE papers SET status='archived' WHERE id=?", (arc,))
    conn.commit()
    # 快照库：清理孤儿 docir（无 active 引用）
    if docir_dir and docir_dir.exists():
        active = {Path(r[0]).stem for r in conn.execute(
            "SELECT docir_path FROM papers WHERE status='active' AND docir_path != ''")}
        removed = 0
        for f in docir_dir.glob("*.json"):
            if f.stem not in active:
                f.unlink()
                removed += 1
        print(f"  孤儿 docir 清理: {removed} 个文件")
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1].parent
    converge(root / "data" / "scholar.db", apply=args.apply, docir_dir=None)
    converge(root / "data-real" / "scholar.db", apply=args.apply,
             docir_dir=root / "data-real" / "docir")
    return 0


if __name__ == "__main__":
    sys.exit(main())
