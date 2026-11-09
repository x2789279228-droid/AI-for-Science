"""对比语料收录（抗幻觉 P0：多篇真实对比的证据基础）。

下载 arXiv 论文 → 入库 → 解析（宿主机引擎，MinerU 可用时质量最优）：
  BERT       1810.04805  （BERT: Pre-training of Deep Bidirectional Transformers）
  PreLN      2002.04745  （On Layer Normalization in the Transformer Architecture）
  ScalingLaws 2001.08361 （Scaling Laws for Neural Language Models, Kaplan et al.）
  GPT3       2005.14165  （Language Models are Few-Shot Learners, Brown et al.）
  —— 命名勘误：2001.08361 是 Kaplan 缩放律而非 GPT（旧注释误写"GPT2 (Few-Shot
  Learners)"双重错误：Few-Shot Learners 是 GPT-3 (2005.14165)）；GPT 系原文以
  GPT-3 论文代表（GPT-2 原文无 arXiv 版，OpenAI 技术报告 PDF 不在 arXiv 通道）。
--snapshot：把对比语料 + UDL 的解析产物固化进 data-real/（演示快照），
  供 make demo 直接恢复多篇对比演示现场。

用法：cd api && uv run python scripts/fetch_corpus.py [--snapshot]
前置：宿主机 .env（MinerU/LLM 可用则质量最优；纯 PyMuPDF 也可入库，评测质量如实下降）。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CORPUS = [
    ("1810.04805", "BERT"),
    ("2002.04745", "PreLN"),
    ("2001.08361", "ScalingLaws"),
    ("2005.14165", "GPT3"),
]
_SNAPSHOT_TABLES = ("papers", "chunks", "chunks_fts", '"references"')


def download_pdf(aid: str, name: str, out_dir: Path, timeout: float = 180.0) -> Path | None:
    """arXiv 双源流式下载（照抄 tools._paper_download 的 URL/校验模式）；失败返回 None。"""
    import httpx

    dest = out_dir / f"{name}_{aid}.pdf"
    if dest.exists() and dest.stat().st_size > 1024:
        print(f"  [DL] {dest.name} 已存在，跳过下载")
        return dest
    sources = [f"https://arxiv.org/pdf/{aid}", f"https://export.arxiv.org/pdf/{aid}"]
    for url in sources:
        try:
            buf = bytearray()
            with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > 100 * 1024 * 1024:
                        raise RuntimeError("超过 100MB 上限")
            if bytes(buf[:5]) != b"%PDF-" or len(buf) < 1024:
                raise RuntimeError(f"非 PDF 内容（{len(buf)}B）")
            dest.write_bytes(bytes(buf))
            print(f"  [DL] {dest.name} ← {url}（{len(buf)} B）")
            return dest
        except Exception as e:  # noqa: BLE001 —— 双源逐个尝试，全失败才放弃
            print(f"  [DL] {url} 失败：{type(e).__name__}: {e}")
    return None


def ingest(pdf_path: Path, timeout_s: float = 1800.0) -> str:
    """入库（file_hash 查重幂等）→ 轮询解析 job 至 done。返回 paper_id。"""
    from app import db, jobs

    res = jobs.upload_pdf(pdf_path.name, pdf_path.read_bytes())
    pid = res["paper_id"]
    if res.get("dedup") == "exact_file":
        print(f"  [IN] {pdf_path.name} 文件级复用（已解析，跳过）")
        return pid
    job_id = res.get("job_id")
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        j = db.get_job(job_id) if job_id else None
        if j is None or j["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(2.0)
    status = (j or {}).get("status", "unknown") if job_id else "no-job"
    if status != "done":
        raise RuntimeError(f"{pdf_path.name} 解析未完成：{status} · "
                           f"{(j or {}).get('message', '')[:120]}")
    print(f"  [IN] {pdf_path.name} → {pid} 解析完成（{int(time.time() - t0)}s）")
    return pid


def snapshot(target_ids: list[str], data_dir: Path, real_dir: Path) -> None:
    """把指定 papers 的解析产物固化进 data-real（db 行合并 + docir/uploads 文件拷贝）。

    每 pid 独立事务（即时 commit）——单篇失败不回滚其余；错误带表名如实上报。"""
    src_db, dst_db = data_dir / "scholar.db", real_dir / "scholar.db"
    for sub in ("docir", "uploads"):
        (real_dir / sub).mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(str(dst_db), timeout=30)
    dst.execute("PRAGMA busy_timeout=30000")
    dst.execute("PRAGMA temp_store=MEMORY")   # FTS 写磁盘临时文件在 TMPDIR=/tmp 环境会失败
    dst.execute("ATTACH DATABASE ? AS src", (str(src_db),))
    errors: list[str] = []
    done = 0
    for pid in target_ids:
        try:
            for table in _SNAPSHOT_TABLES:
                key_col = "id" if table == "papers" else "paper_id"
                src_cols = [r[1] for r in dst.execute(f"PRAGMA src.table_info({table})")]
                dst_cols = [r[1] for r in dst.execute(f"PRAGMA main.table_info({table})")]
                common = [c for c in dst_cols if c in src_cols]
                if not common:
                    continue
                col_list = ",".join(f'"{c}"' for c in common)
                dst.execute(f"DELETE FROM {table} WHERE {key_col}=?", (pid,))
                dst.execute(f"INSERT OR REPLACE INTO {table} ({col_list}) "
                            f"SELECT {col_list} FROM src.{table} WHERE {key_col}=?", (pid,))
            dst.commit()
        except sqlite3.Error as e:
            errors.append(f"{pid}: {e}")
            dst.rollback()
            continue
        row = dst.execute("SELECT docir_path, file_path FROM papers WHERE id=?", (pid,)).fetchone()
        for col in (0, 1):
            p = Path(row[col]) if row and row[col] else None
            if p and p.exists():
                dest = real_dir / p.parent.name / p.name
                if not dest.exists() or dest.stat().st_size != p.stat().st_size:
                    shutil.copy2(p, dest)
        done += 1
        print(f"  [SNAP] {pid} 已固化（docir/uploads/db 行）")
    if errors:
        print(f"  [SNAP] 失败 {len(errors)} 篇：{errors}")
    print(f"  [SNAP] 完成 {done}/{len(target_ids)}")
    dst.commit()
    dst.close()


def main() -> int:
    from app import db  # 依赖宿主机 .env（引擎/LLM 按宿主机配置）

    ap = argparse.ArgumentParser(description="对比语料收录（BERT/PreLN/GPT-2）+ 快照固化")
    ap.add_argument("--snapshot", action="store_true",
                    help="收录完成后把语料 + UDL 固化进 data-real/")
    args = ap.parse_args()

    data_dir = Path(__file__).resolve().parents[1].parent / "data"   # scholar-agent/data
    real_dir = Path(__file__).resolve().parents[1].parent / "data-real"
    corpus_dir = Path(__file__).resolve().parents[3] / "downloads" / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)

    db.init_db()
    target_ids: list[str] = []
    failed: list[str] = []
    for aid, name in CORPUS:
        pdf = download_pdf(aid, name, corpus_dir)
        if pdf is None:
            failed.append(name)
            continue
        try:
            target_ids.append(ingest(pdf))
        except Exception as e:  # noqa: BLE001 —— 单篇失败如实记录，不拖垮其余
            failed.append(f"{name}:{type(e).__name__}")
            print(f"  [IN] {name} 收录失败：{e}")

    if args.snapshot:
        # UDL 固化：工作目录 data/ 已有解析产物（541 页），按标题定位
        rows = db.query("SELECT id FROM papers WHERE title LIKE '%Understanding Deep Learning%' "
                        "AND docir_path != ''")
        udl_ids = [r["id"] for r in rows]
        print(f"[SNAP] 目标：对比语料 {len(target_ids)} 篇 + UDL {len(udl_ids)} 篇")
        if udl_ids:
            target_ids += udl_ids
        if target_ids:
            snapshot(target_ids, data_dir, real_dir)
        else:
            print("[SNAP] 无可固化目标（收录全失败且无 UDL）")
    if failed:
        print(f"[DONE] 收录失败：{failed}")
        return 1
    print(f"[DONE] 对比语料就绪：{len(target_ids)} 篇")
    return 0


if __name__ == "__main__":
    sys.exit(main())
