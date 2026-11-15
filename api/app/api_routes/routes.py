"""REST + SSE 路由（方案 §8 MVP 子集）。"""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from .. import cache, db, jobs, threads_store, trace_store
from ..config import UPLOAD_DIR, settings
from ..llm import client as llm
from ..llm import prompts as prompts_mod

router = APIRouter(prefix="/api")

# ---- 热点只读载荷缓存（写路径 bump revision 整体失效，见 app/cache.py） ----
_papers_cache = cache.TTLCache(maxsize=32, ttl=5.0)
_paper_detail_cache = cache.TTLCache(maxsize=64, ttl=10.0)
_families_cache = cache.TTLCache(maxsize=8, ttl=15.0)
_docir_file_cache = cache.TTLCache(maxsize=16, ttl=120.0)


def _etag_json(data: Any, request: Request) -> Response:
    """热点 GET 统一出口：ETag + Cache-Control: no-cache + If-None-Match → 304。

    选 no-cache 而非 max-age：写操作（上传/删除）后浏览器若用 max-age 缓存会
    脏读 30s 陈旧列表；no-cache 强制每次重验证，304 命中时零下载——
    永不脏读，且配合前端内存缓存层把重复请求收敛为一次 HEAD 级往返。
    ETag 排除 cached/cache_age 等命中元信息（否则 hit/miss 翻转会让 ETag 抖动，
    重验证永远 miss）；body 只序列化一次（Response 直发，不经 JSONResponse 二次编码）。
    """
    volatile: dict[str, Any] = {}
    if isinstance(data, dict):
        for k in ("cached", "cache_age"):
            if k in data:
                volatile[k] = data.pop(k)   # data 为端点新建的包装 dict，弹出安全
    body = json.dumps(data, ensure_ascii=False, default=str)
    etag = f'"{hashlib.md5(body.encode("utf-8")).hexdigest()}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache",
               "X-Data-Revision": str(cache.get_revision())}   # 数据版本可见（审计/演示）
    inm = request.headers.get("if-none-match")
    if inm and etag in inm:   # 容忍多值/弱ETag形式
        return Response(status_code=304, headers=headers)
    if volatile:   # 命中元信息只进响应体，不进 ETag（第二次序列化仅热点小载荷）
        body = json.dumps({**data, **volatile}, ensure_ascii=False, default=str)
    return Response(content=body, media_type="application/json", headers=headers)


# ---------- 双模式（paper=学术文献 | general=普通文档） ----------

DOC_TYPES = ("paper", "general")


def _doc_type_of(row) -> str:
    """行内 doc_type 安全读取（旧库迁移前列缺失或空值时归为 paper）。"""
    try:
        return row["doc_type"] if row["doc_type"] else "paper"
    except (IndexError, KeyError):
        return "paper"


def _require_paper_doc(paper_id: str, action: str) -> None:
    """学术专属端点守卫：普通文档不参与版本治理/去重决策/软归档链。"""
    rows = db.query("SELECT doc_type FROM papers WHERE id=?", (paper_id,))
    if rows and _doc_type_of(rows[0]) == "general":
        raise HTTPException(400, f"普通文档不支持「{action}」——该能力仅对学术文献开放")


# ---------- 请求体模型 ----------

class PaperRename(BaseModel):
    title: str


class DedupDecisionBody(BaseModel):
    decision: str            # cover | keep | coexist
    target_id: str           # 既有论文 id（预警的 existing 侧）


class VersionDiffBody(BaseModel):
    base_id: str             # 旧版本 paper_id


# ---------- papers ----------

@router.get("/families")
def list_families(request: Request):
    """族视图（L8/L9）：family 分组 + 版本链排序。canonical=族内最新 active；
    versions 按 _ver_int 升序；pending_alerts 为该族关联的双向待决策预警数。
    archived 成员包含在内（版本链完整性），前端自行折叠。
    缓存：15s TTL，键含 data_revision（写路径 bump 即失效）。"""
    data, age, hit = _families_cache.get_or_build(cache.rev_key("families"), _build_families)
    return _etag_json({**data, "cached": hit, "cache_age": round(age, 2)}, request)


def _build_families() -> dict:
    rows = db.query("SELECT id,title,version,arxiv_id,family_id,status,page_count,"
                    "created_at FROM papers WHERE family_id!=''")
    if not rows:
        return {"families": []}
    pids = [r["id"] for r in rows]
    qm = ",".join("?" * len(pids))
    pend: dict[str, int] = {}
    for a in db.query(f"SELECT new_paper_id, existing_paper_id FROM dedup_alerts "
                      f"WHERE status='pending' AND (new_paper_id IN ({qm}) "
                      f"OR existing_paper_id IN ({qm}))", (*pids, *pids)):
        for pid in {a["new_paper_id"], a["existing_paper_id"]} & set(pids):
            pend[pid] = pend.get(pid, 0) + 1
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["family_id"], []).append(dict(r))
    out = []
    for fid, members in groups.items():
        members.sort(key=lambda m: (jobs._ver_int(m), m["created_at"]))
        active = [m for m in members if m["status"] == "active"]
        canon = max(active or members, key=jobs._ver_int)
        out.append({
            "family_id": fid,
            "canonical_id": canon["id"],
            "canonical_title": canon["title"],
            "canonical_version": canon["version"] or "",
            "pending_alerts": sum(pend.get(m["id"], 0) for m in members),
            "versions": [{"id": m["id"], "title": m["title"],
                          "version": m["version"] or "", "arxiv_id": m["arxiv_id"],
                          "status": m["status"], "page_count": m["page_count"],
                          "pending_alerts": pend.get(m["id"], 0),
                          "created_at": m["created_at"]} for m in members],
        })
    out.sort(key=lambda f: -(f["versions"][-1]["created_at"] or 0))
    return {"families": out}


@router.post("/papers")
async def upload_paper(file: UploadFile = File(...),                       # noqa: B008 —— FastAPI 惯用法
                       doc_type: str = Form("paper")):
    """上传 PDF。doc_type=paper（学术文献，默认）| general（普通文档）——由前端
    按当前模式（专业/普通）决定，同一文件允许以两种类型各存一份。"""
    if doc_type not in DOC_TYPES:
        raise HTTPException(400, f"doc_type 仅支持 {'|'.join(DOC_TYPES)}")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF")
    content = await file.read()
    if len(content) > 200 * 1024 * 1024:
        raise HTTPException(413, "文件超过 200MB")
    try:
        result = jobs.upload_pdf(file.filename, content, doc_type=doc_type)
    except ValueError as e:   # Round4 安全体检：魔数/大小护栏 → 400（而非 500）
        raise HTTPException(400, str(e)) from e
    return result


@router.post("/papers/batch")
async def upload_papers_batch(files: list[UploadFile] = File(...),         # noqa: B008
                              doc_type: str = Form("paper")):
    """批量上传（P0）：逐文件独立处理，单个失败不影响其他；每文件返回独立结果。"""
    if doc_type not in DOC_TYPES:
        raise HTTPException(400, f"doc_type 仅支持 {'|'.join(DOC_TYPES)}")
    if not files:
        raise HTTPException(400, "未提供文件")
    if len(files) > settings.batch_upload_max:
        raise HTTPException(413, f"单次批量最多 {settings.batch_upload_max} 个文件")
    results: list[dict] = []
    for f in files:
        name = f.filename or "unnamed.pdf"
        item: dict = {"filename": name}
        try:
            if not name.lower().endswith(".pdf"):
                raise HTTPException(400, "仅支持 PDF")
            content = await f.read()
            if len(content) > 200 * 1024 * 1024:
                raise HTTPException(413, "文件超过 200MB")
            item.update(jobs.upload_pdf(name, content, doc_type=doc_type))
        except HTTPException as e:
            item["error"] = e.detail
        except Exception as e:  # noqa: BLE001 —— 单文件失败隔离（批量要求）
            item["error"] = f"{type(e).__name__}: {e}"
        results.append(item)
    return {"results": results, "ok_count": sum(1 for r in results if not r.get("error"))}


def _pending_alert_map(paper_ids: list[str]) -> dict[str, list[dict]]:
    """双向 pending 去重预警 map：{paper_id: [alert...]}，带对方标题/版本与方向。"""
    if not paper_ids:
        return {}
    qm = ",".join("?" * len(paper_ids))
    rows = db.query(
        f"SELECT * FROM dedup_alerts WHERE status='pending' "
        f"AND (new_paper_id IN ({qm}) OR existing_paper_id IN ({qm})) "
        f"ORDER BY created_at DESC", (*paper_ids, *paper_ids))
    pmeta = {r["id"]: r for r in db.query("SELECT id,title,version,arxiv_id FROM papers")}
    out: dict[str, list[dict]] = {}
    for a in rows:
        try:
            breakdown = json.loads(a["breakdown_json"] or "{}")
        except ValueError:
            breakdown = {}
        is_new_side = a["new_paper_id"] in set(paper_ids)
        other_id = a["existing_paper_id"] if is_new_side else a["new_paper_id"]
        # 同一篇既是 alert 的新方又是旧方（对称补警）→ 归一为 new 侧语义
        other = pmeta.get(other_id)
        for pid in {a["new_paper_id"], a["existing_paper_id"]} & set(paper_ids):
            direction = "new" if pid == a["new_paper_id"] else "existing"
            out.setdefault(pid, []).append({
                "id": a["id"], "score": a["score"], "breakdown": breakdown,
                "reason": a["reason"], "direction": direction,
                "other_paper_id": other_id,
                "other_title": other["title"] if other else "",
                "other_version": other["version"] if other else "",
                # 旧口径别名（duplicate_of/title）：官方用例断言与既有前端兼容
                "duplicate_of": other_id,
                "title": other["title"] if other else "",
            })
    return out


@router.get("/papers")
def list_papers(request: Request, include_archived: bool = False, doc_type: str | None = None):
    """文献列表：默认只回 active（软归档过滤）；include_archived=1 时全量并带 status。
    doc_type=paper|general 按模式过滤（缺省不过滤，返回全部并带 doc_type 字段）。
    缓存：5s TTL，键含 data_revision（AppShell 6s 轮询/多页面共享由此收敛）。"""
    if doc_type is not None and doc_type not in DOC_TYPES:
        raise HTTPException(400, f"doc_type 仅支持 {'|'.join(DOC_TYPES)}")
    data, age, hit = _papers_cache.get_or_build(
        cache.rev_key("papers", include_archived, doc_type),
        lambda: _build_papers(include_archived, doc_type))
    return _etag_json({**data, "cached": hit, "cache_age": round(age, 2)}, request)


def _build_papers(include_archived: bool, doc_type: str | None) -> dict:
    conds = []
    if not include_archived:
        conds.append("status='active'")
    if doc_type is not None:
        conds.append("COALESCE(doc_type,'paper')=?")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    params = (doc_type,) if doc_type is not None else ()
    rows = db.query("SELECT id,title,arxiv_id,page_count,version,supersedes,superseded_by,"
                    "family_id,status,created_at,docir_path,abstract,authors_json,meta_source,"
                    "COALESCE(doc_type,'paper') AS doc_type"
                    f" FROM papers {where} ORDER BY created_at DESC", params)
    alerts = _pending_alert_map([r["id"] for r in rows])
    out = []
    for r in rows:
        d = dict(r)
        d["parsed"] = bool(r["docir_path"])
        try:
            d["authors"] = json.loads(d.pop("authors_json") or "[]")
        except ValueError:
            d["authors"] = []
        lst = alerts.get(r["id"], [])
        if lst:
            d["dedup_alerts"] = lst
            d["dedup_alert"] = lst[0]   # 兼容旧前端单数口径
        out.append(d)
    return {"papers": out}


@router.get("/papers/{paper_id}")
def get_paper(paper_id: str, request: Request):
    data, age, hit = _paper_detail_cache.get_or_build(
        cache.rev_key("paper", paper_id), lambda: _build_paper_detail(paper_id))
    if data is None:
        raise HTTPException(404, "not found")
    return _etag_json({**data, "cached": hit, "cache_age": round(age, 2)}, request)


def _build_paper_detail(paper_id: str) -> dict | None:
    rows = db.query("SELECT * FROM papers WHERE id=?", (paper_id,))
    if not rows:
        return None
    p = dict(rows[0])
    p.pop("meta_json", None)
    p.pop("content_sig", None)   # BLOB 不入 JSON（bytes 无法被 jsonable_encoder 编码）
    p.pop("paper_vec", None)
    refs = db.query('SELECT idx,title,arxiv_id,doi FROM "references" WHERE paper_id=? ORDER BY idx',
                    (paper_id,))
    p["references_count"] = len(refs)
    try:
        p["authors"] = json.loads(p.pop("authors_json") or "[]")
    except ValueError:
        p["authors"] = []
    alerts = _pending_alert_map([paper_id]).get(paper_id, [])
    if alerts:
        p["dedup_alerts"] = alerts
        p["dedup_alert"] = alerts[0]
    return p


@router.patch("/papers/{paper_id}")
def rename_paper(paper_id: str, body: PaperRename):
    """改名（CRUD）：papers.title 同步 DocIR meta.title；family 归一重算。"""
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    if not db.query("SELECT id FROM papers WHERE id=?", (paper_id,)):
        raise HTTPException(404, "not found")
    from ..rag import fingerprint
    row = db.query("SELECT arxiv_id,doc_type FROM papers WHERE id=?", (paper_id,))[0]
    fam = ""
    if _doc_type_of(row) == "paper":
        # 普通文档无版本族概念：只改名，不参与 family 归一
        fam = fingerprint.family_id((row["arxiv_id"] or "").split("v")[0], title)
        with db.tx() as c:
            c.execute("UPDATE papers SET title=?, family_id=? WHERE id=?", (title, fam, paper_id))
    else:
        with db.tx() as c:
            c.execute("UPDATE papers SET title=? WHERE id=?", (title, paper_id))
    cache.bump_revision()   # 列表/详情缓存失效
    data = _resolve_paper_data(paper_id)
    if data is not None:
        data.setdefault("meta", {})["title"] = title
        from ..config import DOCIR_DIR
        (DOCIR_DIR / f"{paper_id}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return {"id": paper_id, "title": title, "family_id": fam}


@router.delete("/papers/{paper_id}")
def delete_paper(paper_id: str):
    """硬删除（CRUD 级联）：文件/DocIR/chunks/FTS/refs/alerts/diffs/jobs/translations。"""
    try:
        return jobs.delete_paper(paper_id)
    except LookupError:
        raise HTTPException(404, "not found") from None


@router.post("/papers/{paper_id}/restore")
def restore_paper(paper_id: str):
    """归档恢复：archived → active（保留 supersedes/superseded_by 供版本链展示）。"""
    if not db.query("SELECT id FROM papers WHERE id=?", (paper_id,)):
        raise HTTPException(404, "not found")
    _require_paper_doc(paper_id, "归档恢复（版本链）")
    with db.tx() as c:
        c.execute("UPDATE papers SET status='active', superseded_by='' WHERE id=?", (paper_id,))
    cache.bump_revision()
    return {"id": paper_id, "status": "active"}


@router.post("/papers/{paper_id}/reparse")
def reparse_paper(paper_id: str):
    """重新解析（CRUD）：同篇已有排队/运行中任务则 409；旧 chunks/references 由解析流程自清。"""
    if not db.query("SELECT id,docir_path FROM papers WHERE id=?", (paper_id,)):
        raise HTTPException(404, "not found")
    running = db.query("SELECT id FROM jobs WHERE paper_id=? AND status IN ('queued','running',"
                       "'cancel_requested')", (paper_id,))
    if running:
        raise HTTPException(409, "该文献已有进行中的任务，请先取消或等待完成")
    job_id = jobs.submit_parse(paper_id)
    return {"paper_id": paper_id, "job_id": job_id}


@router.post("/papers/{paper_id}/dedup-decision")
def dedup_decision(paper_id: str, body: DedupDecisionBody):
    """去重三态决策（覆盖/保留/并存，软归档语义）。

    cover=旧 archived+superseded_by→新；keep=新 archived（「仅归档 v1」）；
    coexist=双方 active+family 关联。幂等；决策后双向 pending alert 落终态。
    """
    _require_paper_doc(paper_id, "去重三态决策")
    _require_paper_doc(body.target_id, "去重三态决策")
    try:
        result = jobs.apply_dedup_decision(paper_id, body.target_id, body.decision)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except LookupError:
        raise HTTPException(404, "not found") from None
    result["diff_available"] = bool(
        db.query("SELECT id FROM papers WHERE docir_path!='' AND id IN (?,?)",
                 (paper_id, body.target_id)))
    return result


@router.post("/papers/{paper_id}/version-diff")
def start_version_diff(paper_id: str, body: VersionDiffBody):
    """DiffLens：对 target（本文）与 base（旧版）跑差异总结任务（job 轮询取进度）。"""
    if not db.query("SELECT id,docir_path FROM papers WHERE id=?", (paper_id,)):
        raise HTTPException(404, "target not found")
    if not db.query("SELECT id,docir_path FROM papers WHERE id=?", (body.base_id,)):
        raise HTTPException(404, "base not found")
    _require_paper_doc(paper_id, "版本差异（DiffLens）")
    _require_paper_doc(body.base_id, "版本差异（DiffLens）")
    cached = db.get_version_diff(body.base_id, paper_id)
    if cached:
        return {"cached": True, "diff_id": cached["id"], "job_id": None,
                "stats": cached["stats"]}
    job_id = jobs.submit_version_diff(body.base_id, paper_id)
    return {"cached": False, "job_id": job_id}


@router.get("/papers/{paper_id}/version-diff")
def get_version_diff(paper_id: str, base_id: str):
    cached = db.get_version_diff(base_id, paper_id)
    if not cached:
        raise HTTPException(404, "暂无缓存差异结果（先 POST 启动任务）")
    return {"diff_id": cached["id"], "stats": cached["stats"], "payload": cached["payload"]}


@router.get("/papers/{paper_id}/pdf")
def get_paper_pdf(paper_id: str):
    """原文 PDF（前端 PdfAnchored 画布渲染 + bbox 高亮框的取件源）。

    DB 存的是上传时的宿主绝对路径——容器内该路径不存在，按数据目录幂等回退
    （uploads/{paper_id}.pdf 是 upload_pdf 的确定落点），保证镜像化部署可用。
    """
    rows = db.query("SELECT file_path FROM papers WHERE id=?", (paper_id,))
    if not rows:
        raise HTTPException(404, "not found")
    path = Path(rows[0]["file_path"] or "")
    if not path.exists():
        path = UPLOAD_DIR / f"{paper_id}.pdf"
    if not path.exists():
        raise HTTPException(404, "PDF 文件不存在")
    return FileResponse(path, media_type="application/pdf", filename=path.name)


def _resolve_paper_data(paper_id: str) -> dict | None:
    """P1-1：按 DB docir_path 定位 DocIR，容器内宿主绝对路径不存在时回退数据目录。

    回退点是 write_docir 的确定落点 DOCIR_DIR/{paper_id}.json（与 /pdf 的 uploads 回退
    同一防御思想）。两处皆不存在返回 None（调用方 404）。
    缓存：按 (路径, mtime_ns, size) 键缓存解析后的 dict——os.stat 极便宜，
    命中免去整份 JSON 的 read_text+json.loads（541 页书每次翻页省几十 MB 读盘）；
    reparse 覆盖写盘后 mtime 变化自动失效，无需手动失效。返回值视为只读。
    """
    from ..config import DOCIR_DIR

    rows = db.query("SELECT docir_path FROM papers WHERE id=?", (paper_id,))
    if not rows or not rows[0]["docir_path"]:
        return None
    path = Path(rows[0]["docir_path"] or "")
    if not path.exists():
        path = DOCIR_DIR / f"{paper_id}.json"
    if not path.exists():
        return None
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        hit = _docir_file_cache.get(key)
        if hit is not None:
            return hit[0]
        data = json.loads(path.read_text(encoding="utf-8"))
        _docir_file_cache.set(key, data)
        return data
    except (OSError, ValueError):   # 文件损坏视同未解析
        return None


@router.get("/papers/{paper_id}/figure/{figure_id}")
def get_paper_figure(paper_id: str, figure_id: str):
    """MinerU 裁图（figures[].image_path，校验路径必须落在解析输出目录内）。"""
    data = _resolve_paper_data(paper_id)
    if data is None:
        raise HTTPException(404, "未解析或不存在")
    fig = next((f for f in data.get("figures", []) if f.get("id") == figure_id), None)
    img = fig.get("image_path") if fig else None
    if not img:
        raise HTTPException(404, "figure 无裁图")
    path = Path(img)
    from ..config import DOCIR_DIR
    if not path.exists() or DOCIR_DIR.parent not in path.resolve().parents:
        raise HTTPException(404, "裁图文件不存在")
    return FileResponse(path)


@router.get("/papers/{paper_id}/docir")
def get_docir(paper_id: str, request: Request, page: int | None = None, outline: bool = False):
    """DocIR 按需读取（outline 大纲级 / page 按页）。底层文件缓存见 _resolve_paper_data；
    注意不得 mutate 缓存返回的 dict（页过滤走浅拷贝）。"""
    data = _resolve_paper_data(paper_id)
    if data is None:
        raise HTTPException(404, "未解析或不存在（数据目录回退未命中）")
    if outline:
        # 按需加载（4.3）：超长文档首屏只回大纲级载荷（几 KB），正文按页拉取
        payload = {"meta": data.get("meta", {}),
                   "toc": data.get("toc", []),
                   "references": data.get("references", []),
                   "figures": data.get("figures", []),
                   "pages": sorted({b.get("page", 0) for b in data.get("blocks", [])}),
                   "block_count": len(data.get("blocks", [])),
                   "tables_count": len(data.get("tables", []))}
    elif page is not None:
        # 浅拷贝 + 过滤字段：不触碰缓存对象（此前原地赋值会污染后续 outline 请求）
        payload = {**data,
                   "blocks": [b for b in data.get("blocks", []) if b["page"] == page],
                   "tables": [t for t in data.get("tables", []) if t["page"] == page],
                   "figures": [f for f in data.get("figures", []) if f["page"] == page]}
    else:
        payload = data
    return _etag_json(payload, request)


@router.get("/papers/{paper_id}/search")
def search_paper(paper_id: str, q: str, top_k: int = 10):
    """模块三 D：全文检索（FTS5 chunk 级，CJK bigram 可命中中文）。

    - q 过滤后 <2 字符 → 400；无命中 → 空列表（不 404）；
    - 服务端截 snippet（首个命中词 ±60 字符窗口，纯文本不嵌 HTML——前端 <mark> 自渲染，
      天然防注入）；
    - block_ids 供前端跨页块级跳转（data-bid scrollIntoView）。
    """
    from ..rag import indexing

    if _resolve_paper_data(paper_id) is None:
        raise HTTPException(404, "未解析或不存在")
    query = (q or "").strip()
    if len(query) < 2:
        raise HTTPException(400, "查询过短（≥2 字符）")
    hits = indexing.fts_search([paper_id], query, top_k=max(1, min(top_k, 50)))
    terms = [t for t in "".join(ch if ch.isalnum() else " "
             for ch in indexing.cjk_bigram(query)).split() if t]
    results = []
    for h in hits:
        text = h["text"] or ""
        low = text.lower()
        pos = min((p for p in (low.find(t.lower()) for t in terms) if p >= 0),
                  default=-1)
        start = max(0, pos - 60) if pos >= 0 else 0
        snippet = text[start:start + 140]
        results.append({
            "chunk_id": h["chunk_id"], "page": h["page"],
            "section_path": h["section_path"] or "",
            "snippet": ("…" if start > 0 else "") + snippet
                       + ("…" if start + 140 < len(text) else ""),
            "score": h["score"], "block_ids": h["block_ids"],
        })
    return {"results": results, "total": len(results)}


# ---------- jobs ----------

@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "not found")
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """取消任务（协作式）：queued/running → cancel_requested → 引擎阶段边界终止。

    MinerU 子进程会被 terminate（parsing/mineru_engine）；终态 cancelled。
    """
    job = jobs.cancel_job(job_id)
    if job is None:
        raise HTTPException(404, "not found")
    return job


# ---------- chat（SSE 溯源问答） ----------

class ChatBody(BaseModel):
    question: str
    paper_ids: list[str] = []
    conversation_id: str = ""   # 会话标识：断点续跑按会话归属；空则后端生成并经事件返回
    agent: str = "auto"         # Agent 指定：auto/reader/compare/graph/writer/viz（最高优先级）
    doc_mode: str = "paper"     # 双模式：paper（学术文献）| general（普通文档）——决定工具/意图白名单


@router.post("/chat")
def chat(body: ChatBody):
    import queue as queue_mod
    import threading
    import uuid as uuid_mod

    from ..agents import runner
    from ..agents.state import FORCED_AGENT_VALUES, GENERAL_FORCED_AGENTS

    if body.doc_mode not in DOC_TYPES:
        raise HTTPException(400, f"doc_mode 仅支持 {'|'.join(DOC_TYPES)}")
    # 双模式 Agent 白名单：普通模式无 Graph（引用图谱）/Writer（学术写作）
    allowed_agents = FORCED_AGENT_VALUES if body.doc_mode == "paper" else GENERAL_FORCED_AGENTS
    if body.agent not in allowed_agents:
        raise HTTPException(400, f"doc_mode={body.doc_mode} 下 agent 取值须为 {sorted(allowed_agents)}")

    if body.paper_ids:
        # 混选兜底：普通模式与专业模式的文档不得混在一轮问答里（前端按模式过滤，此为防线）
        qm = ",".join("?" * len(body.paper_ids))
        mismatch = db.query(f"SELECT id FROM papers WHERE id IN ({qm}) "
                            "AND COALESCE(doc_type,'paper')!=?", (*body.paper_ids, body.doc_mode))
        if mismatch:
            raise HTTPException(400, "选文同时包含学术文献与普通文档——请按模式分开提问")
    else:
        # 隐式全库：只取当前模式的 active 文档
        paper_ids = [r["id"] for r in db.query(
            "SELECT id FROM papers WHERE status='active' AND COALESCE(doc_type,'paper')=?",
            (body.doc_mode,))]
    paper_ids = body.paper_ids or paper_ids
    if not paper_ids:
        raise HTTPException(400, "文献库为空，请先上传 PDF")
    paper_cap_note = ""
    if len(paper_ids) > 8:   # 演示护栏：默认选文上限 ≤8 篇（超出截断并如实告知前端）
        paper_cap_note = f"选文超过上限 8 篇，已截断为前 8 篇参与检索（共 {len(paper_ids)} 篇请求）"
        paper_ids = paper_ids[:8]

    conv = body.conversation_id.strip() or uuid_mod.uuid4().hex
    turn = threads_store.next_turn(conv)          # 每轮独立 thread，避免累加键跨轮污染
    thread_id = threads_store.make_thread_id(conv, turn)

    q: queue_mod.Queue = queue_mod.Queue()
    if paper_cap_note:                            # 先入队保证顺序：runner 事件之前到达前端
        q.put(("guardrail", {"message": paper_cap_note, "kind": "paper_cap"}))
    threading.Thread(target=runner.run_stream,
                     args=(body.question.strip(), paper_ids, q),
                     kwargs={"thread_id": thread_id, "conversation_id": conv, "turn": turn,
                             "forced_agent": body.agent, "doc_mode": body.doc_mode},
                     daemon=True).start()

    def sse():
        while True:
            try:
                kind, data = q.get(timeout=300)
            except queue_mod.Empty:
                yield 'event: final\ndata: {"answer":"[执行超时]","claims":[],"mode":"none","guardrails":["timeout"],"trace":[]}\n\n'
                yield "event: done\ndata: {}\n\n"
                return
            payload = json.dumps(data, ensure_ascii=False)
            yield f"event: {kind}\ndata: {payload}\n\n"
            if kind == "done":
                return
    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- 透明度中心 ----------

@router.get("/audit/prompts")
def audit_prompts(limit: int = 50):
    rows = db.query("SELECT * FROM qa_logs ORDER BY ts DESC LIMIT ?", (min(limit, 200),))
    versions = db.query(
        "SELECT COALESCE(NULLIF(prompt_version,''),'legacy') AS version, COUNT(*) AS n "
        "FROM qa_logs GROUP BY version ORDER BY n DESC")
    catalog = prompts_mod.catalog()
    return {"logs": [dict(r) for r in rows],
            "prompt_versions": [dict(r) for r in versions],
            "prompt_catalog": catalog,
            "prompt_variant": settings.prompt_variant,
            "llm_configured": bool(llm._endpoint()),
            "llm_available": llm.available(),
            "dense_enabled": settings.dense_enabled,
            "daily_limit": settings.daily_llm_limit}


# ---------- health ----------

@router.get("/health")
def health():
    ep = llm._endpoint()
    return {"status": "ok", "llm": {"configured": bool(ep),
                                    "available": llm.available(),
                                    "provider": settings.llm_provider or "auto",
                                    "model": ep[2] if ep else ""},
            "dense": settings.dense_enabled,
            "papers": db.query("SELECT COUNT(*) c FROM papers")[0]["c"]}


# ---------- Trace 回放 ----------

# ---------- 健康报告（结果健康评分卡；口径与阈值见 docs/api.md） ----------

_HEALTH_WINDOW_DAYS = 7           # 统计窗口（天）
_HEALTH_MIN_TRACES = 30           # 最小生产样本量；不足时不出总分
_HEALTH_TEST_PREFIXES = (         # 评测/测试轨迹前缀：不计入生产健康分
    "kappa_", "gold_", "bench_", "e2e_", "test_", "demo_",
    "smoke_", "dbg_", "fanout_", "probe", "pytest",
)
_HEALTH_WEIGHTS = {               # 固定权重：缺失维度按 0 计，不做重归一化
    "reliability": 0.20,
    "grounded": 0.20,
    "answer_coverage": 0.05,
    "retrieval": 0.15,
    "parse": 0.15,
    "performance": 0.15,
    "data_health": 0.10,
}


def _clamp01(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 4)


def _wilson_ci(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """比例指标的 Wilson 95% 置信区间（小样本不虚高）。"""
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _is_production_thread(thread_id: str) -> bool:
    """健康分只统计本机运行轨迹：排除离线评测/测试 thread 前缀（无外部用户，如实口径）。"""
    return bool(thread_id) and not thread_id.startswith(_HEALTH_TEST_PREFIXES)


def _percentile(sorted_vals: list[float], p: float) -> float:
    """最近秩百分位（输入需升序；p∈(0,1]）。"""
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, math.ceil(p * len(sorted_vals)) - 1))
    return float(sorted_vals[idx])


def _slo_score(value_ms: float, full_ms: float, zero_ms: float) -> float:
    """延迟 SLO 分段线性：≤full 满分，≥zero 零分，中间线性。"""
    return _clamp01((zero_ms - value_ms) / max(1.0, zero_ms - full_ms))


def _latest_eval_gold() -> tuple[str, dict] | None:
    """docs/acceptance 下按 mtime 取最新 eval_gold.json（兼容 round*/ 与 eval-*/*）。"""
    try:
        acc = Path(__file__).resolve().parents[3] / "docs" / "acceptance"
        candidates = list(acc.glob("*/eval_gold.json")) + list(acc.glob("*/*/eval_gold.json"))
        candidates = [p for p in candidates if p.is_file()]
        if not candidates:
            return None
        path = max(candidates, key=lambda x: x.stat().st_mtime)
        return path.parent.name, json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 容器内无 docs 目录等场景降级
        return None


@router.get("/health-report")
def health_report():
    """结果健康评分卡（只读聚合；口径与阈值见 docs/api.md）。

    2026-11-15 重写要点：
    - 窗口 + 样本：最近 7 天、只统计本机运行轨迹（排除评测/测试 thread 前缀；无外部用户，如实口径）；
      样本 < 30 条时 total 置空（insufficient），避免小样本高分。
    - 失败惩罚：失败 trace / 失败任务进入可靠性分母；拒答/无答案轨迹单列"回答覆盖"。
    - 缺测不重归一化：固定权重，缺失维度按 0 计并列入 missing。
    - 不自证循环：grounded 仅 claims 四态；检索=离线 recall@5 + 在线 verifier 各半；
      性能=窗口 p50/p95 SLO；数据卫生=去重积压/解析失败。
    - 外部校准：最近一轮 eval_gold gates 未全过时总分封顶 0.90，并返回失败项。
    - 工程健康（pytest/e2e）由前端 evidence.ts 单独展示，不并入运行时健康分。
    """
    now = time.time()
    cutoff = now - _HEALTH_WINDOW_DAYS * 86400
    trace_store.init()   # 幂等：确保 trace_events 存在（新库/测试库首访不 500）

    # ---- ① 生产轨迹窗口（逐 trace 聚合；verifier 每 trace 取末次，避免重试加权） ----
    rows = [r for r in db.query(
        "SELECT thread_id, status, events_json, updated_at FROM trace_events "
        "WHERE CAST(updated_at AS REAL) >= ? ORDER BY updated_at DESC", (cutoff,))
        if _is_production_thread(str(r["thread_id"] or ""))]
    claims = {"supported": 0, "needs_check": 0, "unsupported": 0, "absent": 0}
    modes: dict[str, int] = {}
    verifier_pass = verifier_fail = plans = replans = 0
    failed_traces = failed_user_input = answered = 0
    for r in rows:
        if r["status"] == "failed":
            # 失败分类（Round6）：图级异常多为「断点已保存可续跑」的可恢复中断
            # （runner 设计如此），与任务取消同属中断类；仅无法恢复的执行异常
            # 计为可靠性失败。判定：final 无 answer 且 guardrail 含 graph_error。
            try:
                ev = json.loads(r["events_json"] or "[]")
            except ValueError:
                ev = []
            resumable = any(
                isinstance(e, dict) and e.get("kind") == "final"
                and ((e.get("data") or {}).get("resumable") is True) for e in ev)
            if resumable:
                failed_user_input += 1   # 可恢复中断：不计可靠性失败
            else:
                failed_traces += 1
        try:
            events = json.loads(r["events_json"] or "[]")
        except ValueError:
            events = []
        verdict = None
        for e in events:
            if not isinstance(e, dict):
                continue
            if e.get("kind") == "plan":
                plans += 1
                if (e.get("data") or {}).get("round", 1) > 1:
                    replans += 1
            elif e.get("kind") == "verifier":
                verdict = (e.get("data") or {}).get("verdict")
        if verdict == "pass":
            verifier_pass += 1
        elif verdict is not None:
            verifier_fail += 1
        final = next((e.get("data") or {} for e in reversed(events)
                      if isinstance(e, dict) and e.get("kind") == "final"), {})
        mode = final.get("mode") or ("refusal" if final.get("refusal") else "none")
        modes[mode] = modes.get(mode, 0) + 1
        cs = final.get("claims") or []
        if cs:
            answered += 1
        for c in cs:
            k = c.get("status")
            if k in claims:
                claims[k] += 1
    n_traces = len(rows)
    judged = claims["supported"] + claims["needs_check"] + claims["unsupported"]

    # ---- ② 任务失败 / LLM 调用错误（窗口内；LLM 错误作展示与审计，不重复扣分） ----
    # 失败分类（Round6 可信度修复）：cancelled（用户主动取消）、运维事件
    # （server restarted / 重解析超时被取消）不算可靠性失败——只有真实执行
    # 失败（解析/索引/生成异常）进可靠性分母。
    jrows = db.query("SELECT status, message FROM jobs WHERE CAST(created_at AS REAL) >= ?", (cutoff,))
    jobs_total = len(jrows)
    jobs_cancelled = jobs_infra = 0
    jobs_failed = 0
    for r in jrows:
        if r["status"] in ("cancelled", "cancel_requested"):
            jobs_cancelled += 1
        elif r["status"] in ("error", "failed"):
            msg = (r["message"] or "").lower()
            if "server restarted" in msg or "取消" in (r["message"] or ""):
                jobs_infra += 1
            else:
                jobs_failed += 1
    llm_rows = db.query(
        "SELECT COUNT(*) c FROM qa_logs WHERE CAST(ts AS REAL) >= ?", (cutoff,))[0]["c"]
    llm_errors = db.query(
        "SELECT COUNT(*) c FROM qa_logs WHERE CAST(ts AS REAL) >= ? "
        "AND guardrail LIKE 'llm_error:%'", (cutoff,))[0]["c"]
    llm_error_rate = _clamp01(llm_errors / llm_rows) if llm_rows else None

    # ---- ③ 解析质量（页数加权均值 × 质量元数据覆盖率；缺测=0，不假装健康） ----
    papers = db.query("SELECT id, page_count, docir_path FROM papers WHERE docir_path!=''")
    quality_by_paper: list[tuple[int, float]] = []
    cov_vals: list[float] = []
    ref_vals: list[float] = []
    scanned = parse_fail = 0
    page_total = 0
    for pr in papers:
        pages = int(pr["page_count"] or 0) or 1
        page_total += pages
        try:
            data = _resolve_paper_data(pr["id"])
        except Exception:  # noqa: BLE001 —— 单篇 DocIR 异常不阻塞整体
            parse_fail += 1
            continue
        if not data:
            parse_fail += 1
            continue
        q = (data.get("meta") or {}).get("quality") or {}
        vals: list[float] = []
        if isinstance(q.get("text_coverage"), (int, float)):
            vals.append(float(q["text_coverage"]))
            cov_vals.append(float(q["text_coverage"]))
        if isinstance(q.get("reference_parse_rate"), (int, float)):
            vals.append(float(q["reference_parse_rate"]))
            ref_vals.append(float(q["reference_parse_rate"]))
        if q.get("scanned_ratio") and float(q["scanned_ratio"]) > 0.5:
            scanned += 1
        if vals:
            quality_by_paper.append((pages, sum(vals) / len(vals)))
    weight_sum = sum(w for w, _ in quality_by_paper)
    page_weighted = (sum(w * v for w, v in quality_by_paper) / weight_sum
                     if weight_sum else None)
    quality_coverage = len(quality_by_paper) / len(papers) if papers else None
    mean_text_coverage = sum(cov_vals) / len(cov_vals) if cov_vals else None
    mean_ref_parse = sum(ref_vals) / len(ref_vals) if ref_vals else None
    parse_value = None
    if page_weighted is not None and quality_coverage is not None:
        parse_value = _clamp01(page_weighted * quality_coverage)
    parse = None
    if papers:
        parse = {
            "value": parse_value,
            "page_weighted": _clamp01(page_weighted) if page_weighted is not None else None,
            "quality_coverage": (_clamp01(quality_coverage)
                                 if quality_coverage is not None else None),
            "mean_text_coverage": (_clamp01(mean_text_coverage)
                                   if mean_text_coverage is not None else None),
            "mean_ref_parse": _clamp01(mean_ref_parse) if mean_ref_parse is not None else None,
            "papers_with_quality": len(quality_by_paper),
            "papers_total": len(papers),
            "scanned_papers": scanned,
            "parse_fail": parse_fail,
            "page_total": page_total,
        }

    # ---- ④ 性能（窗口内问答 LLM 调用：p50/p95 SLO；均值只作明细） ----
    lat_rows = db.query(
        "SELECT latency_ms FROM qa_logs WHERE CAST(ts AS REAL) >= ? "
        "AND module IN ('qa', 'chat') AND latency_ms > 0 ORDER BY latency_ms", (cutoff,))
    lat = [float(r["latency_ms"]) for r in lat_rows]
    p50 = _percentile(lat, 0.50) if len(lat) >= 20 else None
    p95 = _percentile(lat, 0.95) if len(lat) >= 20 else None
    avg_lat = sum(lat) / len(lat) if lat else 0.0
    performance = None
    if p50 is not None and p95 is not None:
        performance = _clamp01(0.5 * _slo_score(p50, 3000, 10000)
                               + 0.5 * _slo_score(p95, 8000, 25000))

    # ---- ⑤ 数据卫生（陈旧去重积压 + 解析失败；新鲜的 pending 是正常待办不扣分） ----
    dedup_rows = db.query("SELECT status, COUNT(*) c FROM dedup_alerts GROUP BY status")
    dedup = {r["status"]: r["c"] for r in dedup_rows}
    dedup_total = sum(dedup.values())
    dedup_pending = dedup.get("pending", 0)
    stale_pending = db.query(
        "SELECT COUNT(*) c FROM dedup_alerts WHERE status='pending' "
        "AND CAST(created_at AS REAL) < ?", (cutoff,))[0]["c"]
    dedup_fresh = 1.0
    if dedup_total:
        dedup_fresh = 1 - stale_pending / dedup_total
    parse_ok = 1.0
    if papers:
        parse_ok = 1 - parse_fail / len(papers)
    data_health = _clamp01(dedup_fresh * parse_ok)

    # ---- ⑥ 离线金标（最新 eval_gold；gates 未全过时总分封顶 0.90） ----
    eval_info = _latest_eval_gold()
    offline: dict[str, Any] = {"available": False}
    external_cap = None
    if eval_info:
        round_name, egr = eval_info
        gen = egr.get("generative") or {}
        gates = egr.get("gates") or {}
        gates_total = len(gates)
        gates_failed = [k for k, v in gates.items() if not v]
        gates_passed = gates_total - len(gates_failed)
        external_cap = 1.0 if (gates_total and not gates_failed) else 0.90
        offline = {"available": True, "round": round_name,
                   "date": str(egr.get("time", ""))[:10],
                   "gates_passed": gates_passed, "gates_total": gates_total,
                   "gates_failed": gates_failed,
                   "hallucination_rate": gen.get("hallucination_rate"),
                   "supported_ratio": gen.get("supported_ratio"),
                   "anchor_precision": gen.get("anchor_precision"),
                   "quote_unverified_rate": gen.get("quote_unverified_rate"),
                   "recall_at5": egr.get("recall_at5"),
                   "mrr_at5": egr.get("mrr_at5"),
                   "refusal_accuracy": egr.get("refusal_accuracy")}

    # ---- ⑦ 子分（固定权重；缺失=0 并列入 missing，不重归一化） ----
    scores: dict[str, dict] = {}

    def add_score(key: str, value: float | None, formula: str, raw: dict,
                  n: int | None = None, ci: list[float] | None = None) -> None:
        if value is None:
            return
        item: dict[str, Any] = {"value": _clamp01(value), "weight": _HEALTH_WEIGHTS[key],
                                "formula": formula, "raw": raw}
        if n is not None:
            item["n"] = n
        if ci is not None:
            item["ci"] = ci
        scores[key] = item

    attempts = n_traces + jobs_total
    # 失败分类（Round6）：cancelled/运维事件（server restarted 等）不进可靠性分母，
    # 只统计真实执行失败——「重解析超时被取消」「重启自愈」不是系统可靠性问题。
    attempts_real = attempts - jobs_cancelled - jobs_infra
    reliability = (_clamp01(1 - (failed_traces + jobs_failed) / attempts_real)
                   if attempts_real else None)
    add_score(
        "reliability", reliability,
        f"1 - 失败（trace {failed_traces}/{n_traces} + 任务 {jobs_failed}/{jobs_total}）"
        f" · 非失败排除：取消 {jobs_cancelled} / 运维 {jobs_infra}"
        f" · LLM 错误 {llm_errors}/{llm_rows}"
        + (f"（{llm_error_rate:.1%}）" if llm_error_rate is not None else "")
        + f" · 窗口 {_HEALTH_WINDOW_DAYS} 天",
        {"failed_traces": failed_traces, "traces": n_traces,
         "jobs_failed": jobs_failed, "jobs": jobs_total,
         "jobs_cancelled": jobs_cancelled, "jobs_infra": jobs_infra,
         "traces_resumable": failed_user_input,
         "llm_errors": llm_errors, "llm_calls": llm_rows},
        n=attempts_real,
        ci=_wilson_ci(attempts_real - failed_traces - jobs_failed, attempts_real) if attempts_real else None)

    grounded = _clamp01(claims["supported"] / judged) if judged else None
    add_score(
        "grounded", grounded,
        f"supported {claims['supported']} / 判定论断 {judged}"
        f"（absent {claims['absent']} 诚实负向不计入分母；失败/无答案见可靠性/回答覆盖）",
        claims, n=judged, ci=_wilson_ci(claims["supported"], judged) if judged else None)

    coverage = _clamp01(answered / n_traces) if n_traces else None
    add_score(
        "answer_coverage", coverage,
        f"有论断轨迹 {answered}/{n_traces}（no_answer {n_traces - answered}；"
        f"拒答 {modes.get('refusal', 0)} / 抽取式 {modes.get('extractive', 0)}）",
        {"answered": answered, "no_answer": n_traces - answered, "modes": modes},
        n=n_traces, ci=_wilson_ci(answered, n_traces) if n_traces else None)

    online_verifier = (_clamp01(verifier_pass / (verifier_pass + verifier_fail))
                       if (verifier_pass + verifier_fail) else None)
    offline_recall = (float(offline["recall_at5"])
                      if offline.get("available")
                      and isinstance(offline.get("recall_at5"), (int, float)) else None)
    rparts = [v for v in (online_verifier, offline_recall) if v is not None]
    retrieval = _clamp01(sum(rparts) / len(rparts)) if rparts else None
    add_score(
        "retrieval", retrieval,
        f"在线 verifier 通过 {verifier_pass}/{verifier_pass + verifier_fail}"
        + (f" · 离线 recall@5 {offline_recall:.3f}"
           if offline_recall is not None else "")
        + f"（补检索 {replans}/{plans} 轮）",
        {"verifier_pass": verifier_pass, "verifier_fail": verifier_fail,
         "offline_recall_at5": offline_recall, "plans": plans, "replans": replans},
        n=verifier_pass + verifier_fail)

    add_score(
        "parse", parse_value,
        (f"页数加权质量 {page_weighted:.3f} × 质量元数据覆盖 "
         f"{len(quality_by_paper)}/{len(papers)} 篇"
         if page_weighted is not None
         else f"页数加权质量 — × 质量元数据覆盖 {len(quality_by_paper)}/{len(papers)} 篇")
        + (f" · 文本覆盖 {mean_text_coverage:.3f}" if mean_text_coverage is not None else "")
        + (f" · ref 解析 {mean_ref_parse:.3f}" if mean_ref_parse is not None else ""),
        {"page_weighted": parse["page_weighted"] if parse else None,
         "quality_coverage": parse["quality_coverage"] if parse else None,
         "papers_with_quality": len(quality_by_paper), "papers_total": len(papers),
         "scanned_papers": scanned, "parse_fail": parse_fail},
        n=len(quality_by_paper))

    add_score(
        "performance", performance,
        (f"p50 {int(p50)}ms（SLO 3000→10000ms）· p95 {int(p95)}ms"
         f"（SLO 8000→25000ms）· n={len(lat)}")
        if p50 is not None and p95 is not None else "窗口内问答调用样本 <20",
        {"p50_ms": int(p50) if p50 is not None else None,
         "p95_ms": int(p95) if p95 is not None else None,
         "avg_ms": round(avg_lat), "count": len(lat)},
        n=len(lat))

    add_score(
        "data_health", data_health,
        f"陈旧去重积压 {stale_pending}/{dedup_total} · 解析失败 {parse_fail}/{len(papers)} 篇"
        f"（新鲜 pending {dedup_pending - stale_pending} 条属正常待办，不扣分）",
        {"stale_pending": stale_pending, "dedup_total": dedup_total,
         "dedup_pending": dedup_pending, "parse_fail": parse_fail,
         "papers_total": len(papers)}, n=dedup_total)

    missing = [k for k in _HEALTH_WEIGHTS if k not in scores]
    runtime_total = round(sum(s["value"] * s["weight"] for s in scores.values()), 4)
    insufficient = n_traces < _HEALTH_MIN_TRACES
    insufficient_reason = ""
    total = None
    cap_applied = False
    if insufficient:
        insufficient_reason = (f"窗口内生产轨迹 {n_traces} 条 < 最小样本 "
                               f"{_HEALTH_MIN_TRACES} 条，不出总分（明细仍展示）")
    else:
        total = _clamp01(runtime_total)
        if external_cap is not None and total > external_cap:
            total = external_cap
            cap_applied = True

    return {
        "total": total,
        "runtime_total": _clamp01(runtime_total),
        "cap": external_cap,
        "cap_applied": cap_applied,
        "insufficient": insufficient,
        "insufficient_reason": insufficient_reason,
        "window": {"days": _HEALTH_WINDOW_DAYS, "cutoff": round(cutoff, 3),
                   "min_traces": _HEALTH_MIN_TRACES, "traces": n_traces},
        "weights": _HEALTH_WEIGHTS,
        "missing": missing,
        "scores": scores,
        "samples": {"traces": n_traces, "failed": failed_traces,
                    "answered": answered, "no_answer": n_traces - answered,
                    "modes": modes, "jobs": jobs_total, "jobs_failed": jobs_failed,
                    "llm_calls": llm_rows, "llm_errors": llm_errors,
                    "llm_error_rate": llm_error_rate},
        "trace": {"samples": n_traces, "claims": claims,
                  "verifier_pass": verifier_pass, "verifier_fail": verifier_fail,
                  "plans": plans, "replans": replans, "modes": modes},
        "qa_perf": {"count": len(lat), "avg_latency_ms": round(avg_lat),
                    "p50_ms": int(p50) if p50 is not None else None,
                    "p95_ms": int(p95) if p95 is not None else None,
                    "error_rate": (failed_traces / n_traces) if n_traces else None},
        "parse": parse,
        "dedup": {"total": dedup_total, "pending": dedup_pending,
                  "stale_pending": stale_pending,
                  "covered": dedup.get("covered", 0), "kept": dedup.get("kept", 0),
                  "coexisted": dedup.get("coexisted", 0)},
        "offline": offline,
        "external": {**offline, "cap": external_cap} if offline.get("available") else offline,
        # Round6 可信度披露：judge 校准状态（κ 如实，未达 0.6 前明确标注）+
        # 金标三轮趋势（评测轮次关键指标，历史来源 docs/acceptance/）
        "calibration": {
            "judge_kappa": 0.536,
            "judge_note": "LLM judge 校准中（构造真值口径 κ=0.536，目标 ≥0.60；"
                          "FP=0/负类全检出，正类偏保守）",
            "grounded_note": "回答可信=claims 四态口径；supported 已附逐字原文引文",
        },
        "trend": _eval_trend(),
    }


def _eval_trend() -> list[dict]:
    """金标评测轮次趋势（docs/acceptance 下全部 eval_gold.json 按 mtime 升序）。"""
    try:
        acc = Path(__file__).resolve().parents[3] / "docs" / "acceptance"
        out = []
        for path in sorted(list(acc.glob("*/eval_gold.json")) + list(acc.glob("*/*/eval_gold.json")),
                           key=lambda x: x.stat().st_mtime):
            d = json.loads(path.read_text(encoding="utf-8"))
            g = d.get("generative", {})
            out.append({
                "dir": path.parent.name,
                "recall_at5": d.get("recall_at5"),
                "hallucination_rate": g.get("hallucination_rate"),
                "anchor_precision": g.get("anchor_precision"),
            })
        return out[-6:]
    except Exception:  # noqa: BLE001 —— 趋势读取失败不影响健康报告
        return []


@router.get("/qa/recent")
def qa_recent(limit: int = 3):
    """最近问答轻列表（首页工作台左卡数据源，前端改造方案 v1.0 §3.3 唯一后端新增点，只读）。

    从 trace_events 取最近 N 条，逐条从 events 里的 final 事件聚合 ClaimTrace
    四态计数（supported/needs_check/unsupported/absent）。必须声明在
    /qa/{trace_id} 之前，否则 "recent" 会被当作 trace_id 吞并。
    """
    lim = max(1, min(limit, 10))
    rows = db.query(
        "SELECT thread_id, question, status, updated_at, events_json "
        "FROM trace_events ORDER BY updated_at DESC LIMIT ?", (lim,))
    items = []
    for r in rows:
        try:
            events = json.loads(r["events_json"] or "[]")
        except ValueError:
            events = []
        # 事件形状 = {"kind": ..., "data": ...}（trace_store 落盘口径）；final 在最后
        final = next((e.get("data") or {} for e in reversed(events)
                      if isinstance(e, dict) and e.get("kind") == "final"), {})
        counts = {"supported": 0, "needs_check": 0, "unsupported": 0, "absent": 0}
        for c in final.get("claims") or []:
            k = c.get("status")
            if k in counts:
                counts[k] += 1
        items.append({"thread_id": r["thread_id"], "question": r["question"],
                      "status": r["status"], "ts": r["updated_at"],
                      "elapsed": final.get("elapsed"), "claims": counts})
    return {"items": items}


@router.get("/qa/{trace_id}")
def get_qa_trace(trace_id: str):
    """回放一次问答的完整事件流（trace_id = thread_id，含断点续跑并入的事件）。"""
    trace = trace_store.get_trace(trace_id)
    if trace is None:
        raise HTTPException(404, "trace 不存在")
    return trace
