#!/usr/bin/env python
"""A/B 对比测试环境准备（模块七 r9）：把一篇真实论文复制进 seed 演示库。

用途：纵向 A/B 评测需要 chat 全链路可跑（responder 需要真实 chunks 证据），
而 seed 库只有记忆故事线没有论文。本脚本从真实运行库 ATTACH 复制**一篇**
论文（papers/chunks/chunks_fts，含 vec BLOB）到 seed 库——只增不删，可重复执行
（INSERT OR IGNORE）。

用法：
  python api/scripts/prepare_ab_env.py --seed-dir data/seed_mem \
      --real-db data/scholar.db --paper-id p_1264458b503d
"""
from __future__ import annotations

import argparse
import sqlite3
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed-dir", default="data/seed_mem")
    ap.add_argument("--real-db", default="data/scholar.db")
    ap.add_argument("--paper-id", default="")
    args = ap.parse_args()

    # 双连接分批搬运（ATTACH 跨 WAL 库写入会报 unable to open database file）
    real = sqlite3.connect(f"file:{args.real_db}?mode=ro", uri=True)
    seed = sqlite3.connect(f"{args.seed_dir}/scholar.db")

    if not args.paper_id:
        row = real.execute(
            "SELECT p.id FROM papers p JOIN chunks c ON c.paper_id=p.id "
            "WHERE p.status='active' GROUP BY p.id ORDER BY COUNT(c.id) DESC LIMIT 1").fetchone()
        args.paper_id = row[0]
        print(f"[prep] 未指定论文 → 选 chunks 最多的：{args.paper_id}")

    exists = seed.execute("SELECT COUNT(*) FROM papers WHERE id=?",
                          (args.paper_id,)).fetchone()[0]
    if exists:
        n = seed.execute("SELECT COUNT(*) FROM chunks WHERE paper_id=?",
                         (args.paper_id,)).fetchone()[0]
        print(f"[prep] 论文已存在（{n} chunks），跳过")
        print(f"[prep] paper_id={args.paper_id}")
        return 0

    paper_cols = [r[1] for r in real.execute("PRAGMA table_info(papers)")]
    paper = real.execute(f"SELECT {','.join(paper_cols)} FROM papers WHERE id=?",
                         (args.paper_id,)).fetchone()
    seed.execute(f"INSERT INTO papers({','.join(paper_cols)}) "
                 f"VALUES({','.join('?' * len(paper_cols))})", paper)
    chunk_cols = [r[1] for r in real.execute("PRAGMA table_info(chunks)")]
    chunks = real.execute(f"SELECT {','.join(chunk_cols)} FROM chunks WHERE paper_id=?",
                          (args.paper_id,)).fetchall()
    seed.executemany(f"INSERT INTO chunks({','.join(chunk_cols)}) "
                     f"VALUES({','.join('?' * len(chunk_cols))})", chunks)
    fts_rows = real.execute("SELECT chunk_id, paper_id, text_cjk FROM chunks_fts "
                            "WHERE paper_id=?", (args.paper_id,)).fetchall()
    seed.executemany("INSERT INTO chunks_fts(chunk_id, paper_id, text_cjk) VALUES(?,?,?)",
                     fts_rows)
    seed.commit()
    n = seed.execute("SELECT COUNT(*) FROM chunks WHERE paper_id=?",
                     (args.paper_id,)).fetchone()[0]
    title = seed.execute("SELECT title FROM papers WHERE id=?", (args.paper_id,)).fetchone()[0]
    print(f"[prep] 已复制：{title}（{n} chunks）paper_id={args.paper_id}")
    _ = sys
    return 0


if __name__ == "__main__":
    sys.exit(main())
