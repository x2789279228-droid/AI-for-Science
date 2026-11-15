"""清理演示快照 + 重解析 Attention v7 + 固化 data-real（W9）。

一次性运维脚本（可重复执行，幂等）：
  1. 备份 data/scholar.db 与 data/docir 到 data/_backup_<日期>/；
  2. 删除测试残留：Smoke Test Paper、重复 Attention（general）、孤儿 docir；
  3. 用当前解析/元数据管线重解析 Attention v7（MinerU 原生通道可用时）；
  4. 校验：作者/摘要/标题树/公式/references；
  5. rebuild_families()，并把全部 active 论文合并进 data-real 快照。

用法：cd api && uv run python scripts/clean_snapshot.py
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]          # scholar-agent/
DATA = ROOT / "data"
REAL = ROOT / "data-real"
DELETE_IDS = ["p_5138b7b553e7", "p_d57421c72268"]   # Smoke Test Paper / 重复 Attention(general)
REPARSE_IDS = ["p_5e84333e8e1d"]                    # Attention v7（旧 schema 重解析）
ORPHAN_DOCIR = ["p_3326bce4c606.json"]              # 无 papers 行的孤儿产物
# data-real 快照不收录 v1：官方用例 3.1 的演示流程是「库内已有 v7 → 上传 v1 触发语义去重」，
# 若快照自带 v1 会退化为 exact_file 去重，semantic 预警无法演示。
EXCLUDE_FROM_SNAPSHOT = ["p_a19a95dbf468"]
BACKUP = DATA / f"_backup_{time.strftime('%Y%m%d-%H%M%S')}"


def backup() -> None:
    BACKUP.mkdir(parents=True, exist_ok=True)
    src = DATA / "scholar.db"
    if src.exists():
        con = sqlite3.connect(str(src))
        dst = sqlite3.connect(str(BACKUP / "scholar.db"))
        con.backup(dst)
        dst.close()
        con.close()
    docir = DATA / "docir"
    if docir.exists():
        shutil.copytree(docir, BACKUP / "docir", dirs_exist_ok=True)
    print(f"  [BACKUP] {BACKUP}")


def _poll(job_id: str, timeout_s: float = 3600.0) -> dict:
    from app import db
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        job = db.get_job(job_id)
        if job and job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(1.0)
    return {"status": "timeout", "message": "poll timeout"}


def resign_all() -> int:
    """用修复后的 bottom-k MinHash 重算全部论文 content_sig（不动 paper_vec）。"""
    import json

    from app import db
    from app.config import DOCIR_DIR
    from app.rag import fingerprint

    n = 0
    for row in db.query("SELECT id, docir_path FROM papers WHERE status='active'"):
        path = Path(row["docir_path"] or (DOCIR_DIR / f"{row['id']}.json"))
        if not path.exists():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        text = fingerprint.strip_boilerplate(
            [b.get("text") or "" for b in (doc.get("blocks") or [])])
        sig = fingerprint.minhash_sig(text)
        if sig:
            with db.tx() as c:
                c.execute("UPDATE papers SET content_sig=? WHERE id=?", (sig, row["id"]))
            n += 1
    print(f"  [RESIGN] content_sig 重算 {n} 篇")
    return n


def verify(pid: str) -> None:
    import json

    from app import db
    from app.config import DOCIR_DIR
    row = db.query("SELECT id,title,abstract,authors_json,page_count,meta_source "
                   "FROM papers WHERE id=?", (pid,))[0]
    authors = json.loads(row["authors_json"] or "[]")
    refs = db.query('SELECT COUNT(*) c FROM "references" WHERE paper_id=?', (pid,))[0]["c"]
    path = Path(DOCIR_DIR) / f"{pid}.json"
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    blocks = doc.get("blocks") or []
    types: dict[str, int] = {}
    for b in blocks:
        types[b.get("type") or "?"] = types.get(b.get("type") or "?", 0) + 1
    toc = doc.get("toc") or []
    print(f"  [VERIFY] {pid} authors={len(authors)} abstract_len={len(row['abstract'] or '')} "
          f"page_count={row['page_count']} meta_source={row['meta_source']} refs={refs}")
    print(f"  [VERIFY] blocks={len(blocks)} types={types} toc={len(toc)} "
          f"toc_with_id={sum(1 for t in toc if t.get('id'))} "
          f"tables={len(doc.get('tables') or [])} figures={len(doc.get('figures') or [])} "
          f"formulas={types.get('formula', 0)}")


def prune_real(exclude: list[str]) -> None:
    """从 data-real 移除排除论文 + 任何 papers 表不存在的孤儿行/文件。"""
    db_path = REAL / "scholar.db"
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    papers = [dict(r) for r in conn.execute("SELECT id, docir_path, file_path FROM papers")]
    valid = {r["id"] for r in papers} - set(exclude)
    removed = [r for r in papers if r["id"] not in valid]
    for r in removed:
        for col in ("docir_path", "file_path"):
            if r.get(col):
                p = Path(r[col])
                if p.exists() and str(p).startswith(str(REAL)):
                    p.unlink()
    tables = [t["name"] for t in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        if table.startswith("chunks_fts"):
            continue
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        try:
            if table == "papers" and "id" in cols:
                conn.execute("DELETE FROM papers WHERE id NOT IN "
                             f"({','.join('?' * len(valid))})", tuple(valid))
            elif "paper_id" in cols:
                conn.execute(f'DELETE FROM "{table}" WHERE paper_id NOT IN '
                             f"({','.join('?' * len(valid))})", tuple(valid))
        except sqlite3.Error:
            pass
    conn.commit()
    conn.close()
    for sub, suffix in (("docir", ".json"), ("uploads", ".pdf")):
        folder = REAL / sub
        if not folder.exists():
            continue
        for p in folder.glob(f"*{suffix}"):
            pid = p.stem
            if pid not in valid:
                p.unlink()
    print(f"  [PRUNE] data-real 移除 {len(removed)} 篇（{', '.join(r['id'] for r in removed)}）")


def main() -> int:
    from app import db, jobs
    db.init_db()
    print("== 1/5 备份 ==")
    backup()

    print("== 2/5 删除测试残留 ==")
    for pid in DELETE_IDS:
        try:
            jobs.delete_paper(pid)
            print(f"  [DEL] {pid}")
        except LookupError:
            print(f"  [SKIP] {pid} 不存在")
    for name in ORPHAN_DOCIR:
        p = DATA / "docir" / name
        if p.exists():
            p.unlink()
            print(f"  [DEL] docir/{name}")

    print("== 3/5 重解析 Attention v7（MinerU 可用时走原生通道）==")
    for pid in REPARSE_IDS:
        if not db.query("SELECT id FROM papers WHERE id=?", (pid,)):
            print(f"  [SKIP] {pid} 不存在")
            continue
        job_id = jobs.submit_parse(pid)
        job = _poll(job_id)
        print(f"  [PARSE] {pid} status={job.get('status')} "
              f"message={job.get('message', '')[:160]}")

    print("== 4/5 校验与族重建 ==")
    for pid in REPARSE_IDS:
        verify(pid)
    resign_all()
    # 重算签名后重新落去重预警，保证演示口径一致（LLM 灰区复核不可用时自动跳过）
    for pid in ("p_5e84333e8e1d", "p_a19a95dbf468"):
        try:
            res = jobs.post_parse_dedup(pid)
            print(f"  [DEDUP] {pid} -> {json.dumps(res, ensure_ascii=False)[:200]}"
                  if res else f"  [DEDUP] {pid} -> 无候选")
        except Exception as e:  # noqa: BLE001 —— 去重失败不阻塞快照
            print(f"  [DEDUP-WARN] {pid}: {type(e).__name__}: {e}")
    fam = jobs.rebuild_families()
    print(f"  [FAMILY] {fam}")

    print("== 5/5 固化 data-real 快照 ==")
    from scripts.fetch_corpus import snapshot
    active = [r["id"] for r in db.query(
        "SELECT id FROM papers WHERE status='active' ORDER BY created_at")]
    snapshot(active, DATA, REAL)
    prune_real(EXCLUDE_FROM_SNAPSHOT)
    print(f"  [SNAP] active={len(active)} → data-real（排除 {EXCLUDE_FROM_SNAPSHOT}）")

    print("\n== 完成 ==")
    print(f"备份：{BACKUP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
