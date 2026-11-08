"""后台任务队列（方案：解析走后台队列 + 进度条）。

MVP 用进程内线程池实现，接口与 APScheduler/Redis 队列可替换。
上传 → job(parse)：解析 DocIR → 切块 → FTS 索引 → 语义去重预警。
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psutil

from . import db
from .config import DOCIR_DIR, UPLOAD_DIR, settings
from .log import logger
from .parsing.router import parse_document, write_docir
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

        # 内存占用曲线（4.3）：采样线程每 2s 记录 RSS；峰值写进 DocIR meta（解析报告展示）
        proc = psutil.Process()
        mem_curve: list[list[float]] = []
        stop_sampler = threading.Event()

        def _sample_mem() -> None:
            t0 = time.time()
            while True:
                try:   # 先采一次（快解析也要有曲线起点），之后每 2s 一次
                    mem_curve.append([round(time.time() - t0, 1),
                                      round(proc.memory_info().rss / 1048576, 1)])
                except Exception:  # noqa: BLE001, S110 —— 采样失败跳过
                    pass
                if stop_sampler.wait(2.0):
                    break

        sampler = threading.Thread(target=_sample_mem, daemon=True)
        sampler.start()
        docir = parse_document(paper["file_path"], progress=progress, paper_id=paper_id)
        stop_sampler.set()
        sampler.join(timeout=1.0)
        peak_mb = max((m[1] for m in mem_curve), default=0.0)
        docir["meta"]["parse_memory"] = {"peak_mb": peak_mb, "samples": len(mem_curve)}
        write_docir(paper_id, docir)

        db.update_job(job_id, progress=0.88, message="切块与索引")
        index_docir = docir
        long_note = ""
        if docir["meta"].get("long_doc"):
            # 降级矩阵：长文档只对前 N 页做全文 chunk/索引，全书标题树已在 DocIR
            cap = settings.long_doc_index_pages
            index_docir = _index_view(docir, cap)
            long_note = f"（长文档：前 {cap} 页全文索引）"
        chunks = chunk_docir(index_docir)
        n = indexing.index_chunks(paper_id, chunks)
        vec_rows = db.query("SELECT COUNT(*) c FROM chunks WHERE paper_id=? AND vec IS NOT NULL",
                            (paper_id,))
        if indexing._backend_mode() != "off":
            vec_note = (f"，向量 {vec_rows[0]['c']}/{n}"
                        if vec_rows[0]["c"] else f"，向量 0/{n}（{indexing._EMBED_LAST_ERROR or '未知原因'}）")
        else:
            vec_note = ""

        # references 落库
        with db.tx() as c:
            c.execute('DELETE FROM "references" WHERE paper_id=?', (paper_id,))
            for r in docir.get("references", []):
                c.execute('INSERT INTO "references"(id,paper_id,idx,raw,title,arxiv_id,doi)'
                          " VALUES(?,?,?,?,?,?,?)",
                          (db.new_id("r_"), paper_id, r["index"], r["raw"],
                           r.get("title", ""), r.get("arxiv_id", ""), r.get("doi", "")))

        with db.tx() as c:
            # arxiv_id 存基础 id（剥离 vN）；版本号单独入 version 列（去重文案/版本演进用）
            base_arxiv, ver_int = _split_arxiv(docir["meta"].get("arxiv_id", ""))
            ver = f"v{ver_int}" if ver_int else \
                _version_from_name(paper["meta_json"] and json.loads(paper["meta_json"] or "{}")
                                   .get("orig_filename", ""))
            c.execute("UPDATE papers SET title=?, arxiv_id=?, version=?, page_count=?, docir_path=? WHERE id=?",
                      (docir["meta"].get("title", ""), base_arxiv, ver,
                       docir["meta"].get("page_count", 0), str(DOCIR_DIR / f"{paper_id}.json"), paper_id))

        # 内容级去重先于置 done（官方用例 3.1：done 的瞬间 dedup_alert 必须已在；
        # P0-2 根因之二——旧顺序先 done 后去重，前端拿到 done 时预警尚不存在）
        try:
            post_parse_dedup(paper_id)
        except Exception as e:  # noqa: BLE001 —— 去重失败不影响 parse 成功，job message 如实标注
            logger.warning(f"post_parse_dedup 失败（不阻塞 parse）: {type(e).__name__}: {e}",
                           module="dedup")

        db.update_job(job_id, status="done", progress=1.0,
                      message=f"完成：{len(chunks)} chunks{vec_note} / refs {len(docir.get('references', []))}"
                              f" · {docir['meta'].get('parse_engine', '')}{long_note}")
    except Exception as e:  # noqa: BLE001
        db.update_job(job_id, status="error", message=f"{type(e).__name__}: {e}")


# ---------- 语义去重（官方用例 1） ----------

def _title_norm(t: str) -> str:
    return "".join(ch.lower() for ch in t if ch.isalnum())


_ARXIV_FULL = re.compile(r"^(\d{4}\.\d{4,5})(?:v(\d+))?$")


def _index_view(docir: dict, cap: int) -> dict:
    """长文档索引视图：blocks/tables/**toc** 全部截断到前 cap 页（P0-3）。

    旧实现只过滤 blocks/tables——chunking 的章节概览块（summary）直接用 docir["toc"] 页码，
    后段章节会产生 page≥cap 的 summary chunk，官方用例 3.2b 的 max(chunk.page)<120 断言失败。
    """
    return {**docir,
            "blocks": [b for b in docir["blocks"] if b["page"] < cap],
            "tables": [t for t in docir["tables"] if t["page"] < cap],
            "toc": [t for t in docir.get("toc", []) if t.get("page", 0) < cap]}


def _split_arxiv(arxiv_id: str) -> tuple[str, int]:
    """arXiv id → (base, version_int)，无版本号 version=0。"""
    m = _ARXIV_FULL.match((arxiv_id or "").strip())
    if not m:
        return (arxiv_id or ""), 0
    return m.group(1), int(m.group(2) or 0)


def _version_from_name(name: str) -> str:
    """文件名抽版本：优先 arXiv id 后缀（1706.03762v7.pdf → v7），退化 \bvN\b。

    旧正则 `\bv(\\d+)\b` 对 "…03762v7" 不命中（digit→v 无词边界）——官方用例 3.1 根因之一。
    """
    m = re.search(r"(\d{4}\.\d{4,5})v(\d+)", name or "")
    if m:
        return f"v{m.group(2)}"
    m = re.search(r"\bv(\d+)\b", name or "")
    return f"v{m.group(1)}" if m else ""


def check_duplicate(title: str, arxiv_id: str, file_hash: str,
                    exclude_paper_id: str = "", new_version: str = "") -> dict | None:
    """标题归一化 + arXiv id 比对（剥离 vN 后比对，v1/v7 算同源）。

    官方用例 3.1 文案要求：相似度 + 版本号 + 保留建议（版本号新者指引）。
    """
    if not title:
        return None
    nt = _title_norm(title)
    if len(nt) < 8:
        return None
    base_new, ver_new = _split_arxiv(arxiv_id)
    if not ver_new:
        m = re.search(r"v(\d+)$", new_version or "")
        ver_new = int(m.group(1)) if m else 0
    for row in db.query("SELECT id,title,arxiv_id,version FROM papers WHERE id != ?", (exclude_paper_id,)):
        if _title_norm(row["title"]) == nt and len(nt) >= 8:
            base_old, ver_old = _split_arxiv(row["arxiv_id"] or "")
            ver_old = ver_old or int(re.sub(r"\D", "", row["version"] or "") or 0)
            same_arxiv = bool(base_new) and base_new == base_old
            if ver_new and ver_old and ver_new != ver_old:
                if ver_old > ver_new:
                    suggest = f"库中已有版本较新（v{ver_old} > v{ver_new}），建议保留已有版本，新上传仅作历史参照"
                else:
                    suggest = f"新上传版本较新（v{ver_new} > v{ver_old}），建议保留新版本并可归档旧版本"
            else:
                suggest = "版本号未知或相同，请人工核对内容差异"
            return {"duplicate_of": row["id"], "title": row["title"],
                    "existing_version": (f"v{ver_old}" if ver_old else (row["version"] or "")),
                    "new_version": f"v{ver_new}" if ver_new else "",
                    "same_arxiv_id": same_arxiv,
                    "reason": "相似度 100%（标题内容级匹配）"
                              + ("，且 arXiv id 同源" if same_arxiv else "")
                              + f" · {suggest}"}
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
    dup = check_duplicate(p["title"], p["arxiv_id"], p["file_hash"],
                          exclude_paper_id=paper_id, new_version=p["version"] or "")
    if dup:
        with db.tx() as c:
            c.execute("UPDATE papers SET supersedes=? WHERE id=?", (dup["duplicate_of"], paper_id))
        db.create_job("dedup_alert", paper_id=paper_id, payload=dup)
    return dup
