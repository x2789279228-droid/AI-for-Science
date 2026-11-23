"""REST + SSE 路由（方案 §8 MVP 子集）。"""
from __future__ import annotations

import hashlib
import json
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
    thinking: bool = True       # 深度思考（ChatGLM 模式）：responder 流式 reasoning_delta；全局可熔断
    use_memory: bool = True     # 模块七：记忆时间线注入 + memory_brief 开场简报开关


def _probe_doc_scope(paper_ids: list[str], question: str,
                     top_k: int = 24, keep: int = 3) -> list[str] | None:
    """隐式全库范围收窄（v2.3.0）：原问题一次混合检索探查，按每篇命中数取前 keep 篇。

    背景：未勾选文档时全库（≤8 篇）参与 aspects×papers 笛卡尔规划，无关文档
    （如"伴碟"）挤占 8 块证据预算稀释相关证据。命中文档 <2 篇或探查失败返回
    None（保持全库，不阻断——compare 等需要多篇场景不被饿死）。
    """
    from ..rag import indexing  # 惰性导入：routes 顶层不依赖 rag

    try:
        res = indexing.hybrid_search(paper_ids, question, top_k=top_k)
    except Exception:  # noqa: BLE001 —— 探查失败保持全库
        return None
    counts: dict[str, int] = {}
    for h in res.get("hits", []):
        pid = h.get("paper_id", "")
        if pid in set(paper_ids):
            counts[pid] = counts.get(pid, 0) + 1
    if len(counts) < 2:
        return None
    ranked = sorted(counts, key=lambda p: -counts[p])[:keep]
    return ranked if 0 < len(ranked) < len(paper_ids) else None


@router.post("/chat")
def chat(body: ChatBody):
    import queue as queue_mod
    import threading
    import uuid as uuid_mod

    from ..agents import runner
    from ..agents.state import FORCED_AGENT_VALUES, GENERAL_FORCED_AGENTS

    if body.doc_mode not in DOC_TYPES:
        raise HTTPException(400, f"doc_mode 仅支持 {'|'.join(DOC_TYPES)}")
    # 空问题入口拦截（P1）：空输入此前会流进执行链失败并留下 resumable 轨迹，
    # 把「用户输入错误」伪装成系统中断——400 直接拒绝，不产生失败样本
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(400, "问题不能为空")
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
    # v2.3.0 隐式全库自动收窄：无关文档挤占证据预算（显式勾选绝不干预）
    scope_note = ""
    if not body.paper_ids and len(paper_ids) > 3:
        pruned = _probe_doc_scope(paper_ids, question)
        if pruned:
            scope_note = (f"未指定文档：已按问题相关度自动收窄到 {len(pruned)} 篇"
                          f"（全库 {len(paper_ids)} 篇参与探查，可在文献库勾选指定文档）")
            paper_ids = pruned
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
    if scope_note:                                # v2.3.0 范围收窄如实告知（可溯源）
        q.put(("guardrail", {"message": scope_note, "kind": "doc_scope_pruned"}))
    from ..agents import cancel  # 停止生成注册表：POST /chat/{thread_id}/stop 置位
    cancel.register(thread_id)
    # meta 先行下发 thread_id：流式期间前端即可发起停止（无需等 final 才知道线程）
    q.put(("meta", {"thread_id": thread_id, "conversation_id": conv}))
    # 模块七：开场研究简报（清单 §2.7）——meta 后直发，读缓存零 LLM 阻塞；
    # 空简报 {empty:true} 前端不渲染；use_memory=false 整条链不下发
    if body.use_memory:
        from ..log import logger
        from ..memory.briefing import cached_brief
        try:
            q.put(("memory_brief", cached_brief(conversation_id=conv)))
        except Exception as e:  # noqa: BLE001 —— 简报失败绝不阻断问答流（降级为无简报）
            logger.warning(f"开场简报构建失败，降级为无简报: {type(e).__name__}: {e}",
                           module="memory")
    threading.Thread(target=runner.run_stream,
                     args=(question, paper_ids, q),
                     kwargs={"thread_id": thread_id, "conversation_id": conv, "turn": turn,
                             "forced_agent": body.agent, "doc_mode": body.doc_mode,
                             "thinking": body.thinking, "use_memory": body.use_memory},
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

# ---------- 健康报告（结果健康评分卡；聚合实现已迁至 api_routes/health_report.py） ----------

@router.get("/health-report")
def health_report():
    """结果健康评分卡（只读聚合；口径与阈值见 docs/api.md）。

    聚合实现模块化迁出至 app/api_routes/health_report.py（build_health_report）；
    本端点只保留路由注册与依赖注入——resolve_paper_data 以参数注入且在调用时
    解析 routes 全局名，测试 patch routes._resolve_paper_data 依旧生效。
    """
    from .health_report import build_health_report

    return build_health_report(_resolve_paper_data)


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
