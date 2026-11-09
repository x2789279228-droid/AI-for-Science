"""Phase 0：用修复后的 references 解析器重建 data-real 快照。

对既有 4 篇 references 脏块论文（UDL/PreLN/BERT/ScalingLaws）：
删除 → 重新上传解析（MinerU；UDL 与原快照一致走 PyMuPDF）→ 固化 data-real。
Attention（p_6825071792e4，engine=None 演示件）refs 本就正确，不动。

用法：cd api && uv run python scripts/reparse_snapshot.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# (paper_id, 使用 MinerU？)  UDL 541 页原为 PyMuPDF 通道，保持一致且省时
TARGETS = [
    ("p_afd055b62665", True),    # BERT
    ("p_967fade5ae96", True),    # PreLN
    ("p_ddeefd016cbe", True),    # Scaling Laws
    ("p_093639f6b110", False),   # UDL（PyMuPDF）
]


def _poll(job_id: str | None, paper: str, timeout_s: float = 2400.0) -> str:
    from app import db
    if not job_id:
        return "no-job"
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        j = db.get_job(job_id)
        if j is None or j["status"] in ("done", "error", "cancelled"):
            return (j or {}).get("status", "unknown")
        time.sleep(3.0)
    return "timeout"


def main() -> int:
    from app import db, jobs
    from app.config import settings
    from scripts.fetch_corpus import snapshot

    data_dir = Path(__file__).resolve().parents[1].parent / "data"
    real_dir = Path(__file__).resolve().parents[1].parent / "data-real"
    db.init_db()
    # 清掉被强杀进程残留的脏 WAL（此前 taskkill dev server 后出现 unable to open database file）
    db.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")

    done: list[str] = []
    failed: list[str] = []
    for pid, use_mineru in TARGETS:
        pdf = next((p / "uploads" / f"{pid}.pdf" for p in (real_dir, data_dir)
                    if (p / "uploads" / f"{pid}.pdf").exists()), None)
        if pdf is None:
            failed.append(f"{pid}:pdf-missing")
            continue
        try:
            rows = db.query("SELECT id FROM papers WHERE id=?", (pid,))
            if rows:
                jobs.delete_paper(pid)
                print(f"  [DEL] {pid} 旧解析已删除")
            prev = settings.mineru_enabled
            if not use_mineru:
                settings.mineru_enabled = "off"
            try:
                res = jobs.upload_pdf(pdf.name, pdf.read_bytes())
                new_pid = res["paper_id"]           # 重新上传生成新 id（file_hash 去重则复用）
                status = ("done" if res.get("dedup") == "exact_file"
                          else _poll(res.get("job_id"), new_pid))
            finally:
                settings.mineru_enabled = prev
            if status != "done":
                failed.append(f"{pid}:{status}")
                print(f"  [IN] {pid} 解析未完成：{status}")
                continue
            n = db.query('SELECT COUNT(*) c FROM "references" WHERE paper_id=?',
                         (new_pid,))[0]["c"]
            print(f"  [IN] {pid} → {new_pid} 重解析完成，references={n}")
            done.append(new_pid)
        except Exception as e:  # noqa: BLE001 —— 单篇失败不拖垮其余
            failed.append(f"{pid}:{type(e).__name__}:{e}")
            print(f"  [IN] {pid} 失败：{e}")

    if done:
        snapshot(done, data_dir, real_dir)
        # data-real 清理旧脏块 id（行 + docir/uploads 文件）——新 id 已固化，旧 id 不残留
        import sqlite3
        stale = [pid for pid, _ in TARGETS if pid not in done]
        dst = sqlite3.connect(str(real_dir / "scholar.db"), timeout=30)
        for sid in stale:
            for table in ("papers", "chunks", "chunks_fts", '"references"'):
                key = "id" if table == "papers" else "paper_id"
                dst.execute(f"DELETE FROM {table} WHERE {key}=?", (sid,))
            for sub in ("docir", "uploads"):
                (real_dir / sub / f"{sid}.json").unlink(missing_ok=True)
                (real_dir / sub / f"{sid}.pdf").unlink(missing_ok=True)
        dst.commit()
        dst.close()
        print(f"  [CLEAN] data-real 旧 id 已清理：{stale}")

    print("[DONE]", "ok:", done, "failed:", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
