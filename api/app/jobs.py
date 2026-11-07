"""后台任务队列（方案：解析走后台队列 + 进度条）。

MVP 用进程内线程池实现，接口与 APScheduler/Redis 队列可替换。
上传 → job(parse)：解析 DocIR → 切块 → FTS 索引 → 语义去重预警。
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import db
from .config import DOCIR_DIR, UPLOAD_DIR, settings
from .parsing.pipeline import file_sha256, parse_pdf
from .rag import indexing
from .rag.chunking import chunk_docir

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")


def start() -> None:
    """应用启动时调用。"""
    # 重启自愈：把上次中断的 running/queued 重置
    with db.tx() as c:
        c.execute("UPDATE jobs SET status='error', message='server restarted' "
                  "WHERE status IN ('queued','running')")


def submit_parse(paper_id: str) -> str:
    job_id = db.create_job("parse", paper_id=paper_id)
    _pool.submit(_run_parse, job_id, paper_id)
    return job_id


def _run_parse(job_id: str, paper_id: str) -> None:
    db.update_job(job_id, status="running", progress=0.02, message="开始解析")
    papers = db.query("SELECT * FROM papers WHERE id=?", (paper_id,))
    if not papers:
        db.update_job(job_id, status="error", message="paper not found")
        return
    paper = papers[0]
    try:
        def progress(frac: float, msg: str) -> None:
            db.update_job(job_id, progress=round(min(frac, 0.99), 3), message=msg)

        docir = parse_pdf(paper["file_path"], progress=progress)
        (DOCIR_DIR / f"{paper_id}.json").write_text(
            json.dumps(docir, ensure_ascii=False), encoding="utf-8")

        db.update_job(job_id, progress=0.88, message="切块与索引")
        chunks = chunk_docir(docir)
        n = indexing.index_chunks(paper_id, chunks)

        # references 落库
        with db.tx() as c:
            c.execute('DELETE FROM "references" WHERE paper_id=?', (paper_id,))
            for r in docir.get("references", []):
                c.execute('INSERT INTO "references"(id,paper_id,idx,raw,title,arxiv_id,doi)'
                          " VALUES(?,?,?,?,?,?,?)",
                          (db.new_id("r_"), paper_id, r["index"], r["raw"],
                           r.get("title", ""), r.get("arxiv_id", ""), r.get("doi", "")))

        with db.tx() as c:
            c.execute("UPDATE papers SET title=?, arxiv_id=?, page_count=?, docir_path=? WHERE id=?",
                      (docir["meta"].get("title", ""), docir["meta"].get("arxiv_id", ""),
                       docir["meta"].get("page_count", 0), str(DOCIR_DIR / f"{paper_id}.json"), paper_id))

        db.update_job(job_id, status="done", progress=1.0,
                      message=f"完成：{len(chunks)} chunks / refs {len(docir.get('references', []))}")
        post_parse_dedup(paper_id)  # 内容级去重（官方用例 1：先 v7 后 v1）
    except Exception as e:  # noqa: BLE001
        db.update_job(job_id, status="error", message=f"{type(e).__name__}: {e}")


# ---------- 语义去重（官方用例 1） ----------

def _title_norm(t: str) -> str:
    return "".join(ch.lower() for ch in t if ch.isalnum())


def check_duplicate(title: str, arxiv_id: str, file_hash: str) -> dict | None:
    """标题归一化 + arXiv id 辅助比对。MVP 字符级；后续可升级 embedding 余弦。"""
    if not title:
        return None
    nt = _title_norm(title)
    if len(nt) < 8:
        return None
    for row in db.query("SELECT id,title,arxiv_id,version FROM papers WHERE id != ''"):
        if _title_norm(row["title"]) == nt and len(nt) >= 8:
            same_arxiv = bool(arxiv_id) and arxiv_id == row["arxiv_id"]
            return {"duplicate_of": row["id"], "title": row["title"],
                    "existing_version": row["version"] or "", "same_arxiv_id": same_arxiv,
                    "reason": "标题内容级匹配" + ("，且 arXiv id 相同" if same_arxiv else "")}
    return None


def upload_pdf(filename: str, content: bytes) -> dict:
    """保存文件 → 查重 → 建 paper 记录 → 提交解析任务。"""
    import hashlib

    h = hashlib.sha256(content).hexdigest()
    paper_id = db.new_id("p_")
    dest = UPLOAD_DIR / f"{paper_id}.pdf"
    dest.write_bytes(content)

    exact = db.query("SELECT id,title FROM papers WHERE file_hash=?", (h,))
    if exact:  # 文件级完全相同 → 直接复用
        return {"paper_id": exact[0]["id"], "dedup": "exact_file",
                "existing_title": exact[0]["title"], "job_id": None}

    with db.tx() as c:
        c.execute("INSERT INTO papers(id,title,file_path,file_hash,created_at,meta_json)"
                  " VALUES(?,?,?,?,?,?)",
                  (paper_id, filename.rsplit(".", 1)[0], str(dest), h, db.now(),
                   json.dumps({"orig_filename": filename}, ensure_ascii=False)))
    job_id = submit_parse(paper_id)
    return {"paper_id": paper_id, "dedup": None, "job_id": job_id}


# ---------- 事后去重钩子（解析完成拿到标题后） ----------

def post_parse_dedup(paper_id: str) -> dict | None:
    """解析完成后内容级查重（官方用例 1：先 v7 后 v1）。"""
    rows = db.query("SELECT * FROM papers WHERE id=?", (paper_id,))
    if not rows:
        return None
    p = rows[0]
    dup = check_duplicate(p["title"], p["arxiv_id"], p["file_hash"])
    if dup:
        with db.tx() as c:
            c.execute("UPDATE papers SET supersedes=? WHERE id=?", (dup["duplicate_of"], paper_id))
        db.create_job("dedup_alert", paper_id=paper_id, payload=dup)
    return dup
