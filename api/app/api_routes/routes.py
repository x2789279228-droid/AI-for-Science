"""REST + SSE 路由（方案 §8 MVP 子集）。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .. import db, jobs, threads_store, trace_store
from ..config import UPLOAD_DIR, settings
from ..llm import client as llm
from ..llm import prompts as prompts_mod

router = APIRouter(prefix="/api")


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
def list_families():
    """族视图（L8/L9）：family 分组 + 版本链排序。canonical=族内最新 active；
    versions 按 _ver_int 升序；pending_alerts 为该族关联的双向待决策预警数。
    archived 成员包含在内（版本链完整性），前端自行折叠。"""
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
async def upload_paper(file: UploadFile = File(...)):  # noqa: B008 —— FastAPI 惯用法
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF")
    content = await file.read()
    if len(content) > 200 * 1024 * 1024:
        raise HTTPException(413, "文件超过 200MB")
    try:
        result = jobs.upload_pdf(file.filename, content)
    except ValueError as e:   # Round4 安全体检：魔数/大小护栏 → 400（而非 500）
        raise HTTPException(400, str(e)) from e
    return result


@router.post("/papers/batch")
async def upload_papers_batch(files: list[UploadFile] = File(...)):  # noqa: B008
    """批量上传（P0）：逐文件独立处理，单个失败不影响其他；每文件返回独立结果。"""
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
            item.update(jobs.upload_pdf(name, content))
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
def list_papers(include_archived: bool = False):
    """文献列表：默认只回 active（软归档过滤）；include_archived=1 时全量并带 status。"""
    where = "" if include_archived else "WHERE status='active'"
    rows = db.query("SELECT id,title,arxiv_id,page_count,version,supersedes,superseded_by,"
                    "family_id,status,created_at,docir_path,abstract,authors_json,meta_source"
                    f" FROM papers {where} ORDER BY created_at DESC")
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
def get_paper(paper_id: str):
    rows = db.query("SELECT * FROM papers WHERE id=?", (paper_id,))
    if not rows:
        raise HTTPException(404, "not found")
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
    base_arxiv = db.query("SELECT arxiv_id FROM papers WHERE id=?", (paper_id,))[0]["arxiv_id"]
    fam = fingerprint.family_id((base_arxiv or "").split("v")[0], title)
    with db.tx() as c:
        c.execute("UPDATE papers SET title=?, family_id=? WHERE id=?", (title, fam, paper_id))
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
    with db.tx() as c:
        c.execute("UPDATE papers SET status='active', superseded_by='' WHERE id=?", (paper_id,))
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
        return json.loads(path.read_text(encoding="utf-8"))
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
def get_docir(paper_id: str, page: int | None = None, outline: bool = False):
    data = _resolve_paper_data(paper_id)
    if data is None:
        raise HTTPException(404, "未解析或不存在（数据目录回退未命中）")
    if outline:
        # 按需加载（4.3）：超长文档首屏只回大纲级载荷（几 KB），正文按页拉取
        return {"meta": data.get("meta", {}),
                "toc": data.get("toc", []),
                "references": data.get("references", []),
                "figures": data.get("figures", []),
                "pages": sorted({b.get("page", 0) for b in data.get("blocks", [])}),
                "block_count": len(data.get("blocks", [])),
                "tables_count": len(data.get("tables", []))}
    if page is not None:
        data["blocks"] = [b for b in data["blocks"] if b["page"] == page]
        data["tables"] = [t for t in data["tables"] if t["page"] == page]
        data["figures"] = [f for f in data["figures"] if f["page"] == page]
    return data


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


@router.post("/chat")
def chat(body: ChatBody):
    import queue as queue_mod
    import threading
    import uuid as uuid_mod

    from ..agents import runner
    from ..agents.state import FORCED_AGENT_VALUES

    if body.agent not in FORCED_AGENT_VALUES:
        raise HTTPException(400, f"agent 取值须为 {sorted(FORCED_AGENT_VALUES)}")

    paper_ids = body.paper_ids or [r["id"] for r in db.query("SELECT id FROM papers WHERE status='active'")]
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
                             "forced_agent": body.agent},
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

@router.get("/health-report")
def health_report():
    """系统健康报告（健康中心页数据源，只读聚合，公式随返回值下发保证评分可审计）。

    五个子分（溯源可信/幻觉抑制/检索充分/解析覆盖/性能）全部由运行时数据计算；
    工程健康子分（pytest/e2e 离线门禁）由前端静态注入，不在此计算。
    每个子分附 formula 字符串与原始计数——评分本身可审计。
    """
    def _clamp01(x: float) -> float:
        return round(max(0.0, min(1.0, x)), 4)

    # ---- ① 溯源可信 + ② 幻觉抑制（实时口径：最近 30 条 trace 的 final.claims 四态） ----
    rows = db.query(
        "SELECT thread_id, status, events_json FROM trace_events "
        "ORDER BY updated_at DESC LIMIT 30")
    claims = {"supported": 0, "needs_check": 0, "unsupported": 0, "absent": 0}
    verifier_pass = verifier_fail = 0
    plans = replans = 0
    samples = 0
    for r in rows:
        samples += 1
        try:
            events = json.loads(r["events_json"] or "[]")
        except ValueError:
            continue
        for e in events:
            if not isinstance(e, dict):
                continue
            if e.get("kind") == "verifier":
                ev = e.get("data") or {}
                if ev.get("verdict") == "pass":
                    verifier_pass += 1
                else:
                    verifier_fail += 1
            elif e.get("kind") == "plan":
                plans += 1
                if (e.get("data") or {}).get("round", 1) > 1:
                    replans += 1
        final = next((e.get("data") or {} for e in reversed(events)
                      if isinstance(e, dict) and e.get("kind") == "final"), {})
        for c in final.get("claims") or []:
            k = c.get("status")
            if k in claims:
                claims[k] += 1
    judged = claims["supported"] + claims["needs_check"] + claims["unsupported"]
    trace_trust = _clamp01(claims["supported"] / judged) if judged else None
    halluc = None
    if judged:
        halluc = _clamp01(1 - claims["unsupported"] / judged)

    # ---- ③ 检索充分（离线 recall/mrr 优先 + 实时 verifier 通过率） ----
    # ---- ④ 解析覆盖（遍历 papers 的 DocIR meta.quality 均值） ----
    papers = db.query("SELECT id, docir_path FROM papers WHERE docir_path!=''")
    cov_vals: list[float] = []
    ref_vals: list[float] = []
    scanned = 0
    parse_fail = 0
    for pr in papers:
        try:
            data = _resolve_paper_data(pr["id"])
        except Exception:  # noqa: BLE001 —— 单篇 DocIR 异常不阻塞整体
            parse_fail += 1
            continue
        if not data:
            continue
        q = (data.get("meta") or {}).get("quality") or {}
        if isinstance(q.get("text_coverage"), (int, float)):
            cov_vals.append(float(q["text_coverage"]))
        if isinstance(q.get("reference_parse_rate"), (int, float)):
            ref_vals.append(float(q["reference_parse_rate"]))
        if q.get("scanned_ratio") and float(q["scanned_ratio"]) > 0.5:
            scanned += 1
    parse = None
    if cov_vals or ref_vals:
        mc = sum(cov_vals) / len(cov_vals) if cov_vals else None
        mr = sum(ref_vals) / len(ref_vals) if ref_vals else None
        parts = [v for v in (mc, mr) if v is not None]
        parse = _clamp01(sum(parts) / len(parts)) if parts else None
        parse = {"mean_text_coverage": _clamp01(mc) if mc is not None else None,
                 "mean_ref_parse": _clamp01(mr) if mr is not None else None,
                 "papers_with_quality": len(parts), "papers_total": len(papers),
                 "scanned_papers": scanned, "parse_fail": parse_fail,
                 "value": parse}

    # ---- ⑤ 性能（qa_logs 延迟/错误） ----
    perf_row = db.query(
        "SELECT COUNT(*) c, COALESCE(AVG(latency_ms),0) avg_ms FROM qa_logs "
        "WHERE module='chat'")[0]
    failed_qa = db.query(
        "SELECT COUNT(*) c FROM trace_events WHERE status='failed'")[0]["c"]
    done_qa = db.query(
        "SELECT COUNT(*) c FROM trace_events WHERE status='done'")[0]["c"]
    error_rate = (failed_qa / (failed_qa + done_qa)) if (failed_qa + done_qa) else None
    avg_ms = float(perf_row["avg_ms"])
    performance = _clamp01((15000 - avg_ms) / 12000) if avg_ms else None  # ≤3s 满分,线性至 15s

    # ---- 去重与数据健康 ----
    dedup_rows = db.query("SELECT status, COUNT(*) c FROM dedup_alerts "
                          "GROUP BY status")
    dedup = {r["status"]: r["c"] for r in dedup_rows}
    dedup_total = sum(dedup.values())
    dedup_pending = dedup.get("pending", 0)

    # ---- 离线门禁（docs/acceptance 最近一轮 eval_gold.json） ----
    offline = {"available": False}
    try:
        acc_dir = Path(__file__).resolve().parents[3] / "docs" / "acceptance"
        rounds = sorted(acc_dir.glob("round*"))
        if rounds:
            eg = rounds[-1] / "eval_gold.json"
            if eg.exists():
                d = json.loads(eg.read_text(encoding="utf-8"))
                g = d.get("generative") or {}
                offline = {"available": True,
                           "round": rounds[-1].name,
                           "date": str(d.get("time", ""))[:10],
                           "hallucination_rate": g.get("hallucination_rate"),
                           "supported_ratio": g.get("supported_ratio"),
                           "anchor_precision": g.get("anchor_precision"),
                           "quote_unverified_rate": g.get("quote_unverified_rate"),
                           "recall_at5": d.get("recall_at5"),
                           "mrr_at5": d.get("mrr_at5"),
                           "refusal_accuracy": d.get("refusal_accuracy")}
    except Exception:  # noqa: BLE001 —— 容器内无 docs 目录等场景,离线子分降级
        offline = {"available": False}

    # ---- 子分与权重（工程健康由前端静态注入,权重 15%） ----
    scores = {}
    weights = {"trace_trust": 0.30, "hallucination_ctrl": 0.25, "retrieval": 0.15,
               "parse": 0.15, "performance": 0.15}
    if trace_trust is not None:
        scores["trace_trust"] = {
            "value": trace_trust, "weight": weights["trace_trust"],
            "formula": f"supported {claims['supported']} / 判定论断 {judged}"
                       f"（absent {claims['absent']} 诚实负向不计入分母）",
            "raw": claims}
    if halluc is not None:
        scores["hallucination_ctrl"] = {
            "value": halluc, "weight": weights["hallucination_ctrl"],
            "formula": f"1 - unsupported {claims['unsupported']} / {judged}"
                       + (f"（离线对照 eval hallucination_rate="
                          f"{offline.get('hallucination_rate')}）"
                          if offline.get("available") else "（实时口径）"),
            "raw": claims}
    if verifier_pass + verifier_fail:
        retrieval = _clamp01(verifier_pass / (verifier_pass + verifier_fail))
        scores["retrieval"] = {
            "value": retrieval, "weight": weights["retrieval"],
            "formula": f"Verifier 通过 {verifier_pass} / {verifier_pass + verifier_fail}"
                       f"（补检索触发 {replans}/{plans}）"
                       + (f"（离线 recall@5={offline.get('recall_at5')}）"
                          if offline.get("available") else ""),
            "raw": {"pass": verifier_pass, "fail": verifier_fail,
                    "replans": replans, "plans": plans}}
    if isinstance(parse, dict) and parse.get("value") is not None:
        scores["parse"] = {
            "value": parse["value"], "weight": weights["parse"],
            "formula": f"mean(text_coverage {parse['mean_text_coverage']}"
                       f", ref_parse {parse['mean_ref_parse']}) · 覆盖 "
                       f"{parse['papers_with_quality']}/{parse['papers_total']} 篇",
            "raw": parse}
    if performance is not None:
        scores["performance"] = {
            "value": performance, "weight": weights["performance"],
            "formula": f"平均延迟 {int(avg_ms)}ms（≤3000ms 满分,线性至 15000ms）"
                       f" · 失败率 {error_rate if error_rate is not None else '无数据'}",
            "raw": {"avg_ms": int(avg_ms), "error_rate": error_rate,
                    "qa_count": perf_row["c"]}}

    total = None
    if scores:
        w_used = sum(s["weight"] for s in scores.values())
        total = _clamp01(sum(s["value"] * s["weight"] for s in scores.values()) / w_used)

    return {"total": total, "scores": scores,
            "trace": {"samples": samples, "claims": claims,
                      "verifier_pass": verifier_pass, "verifier_fail": verifier_fail,
                      "plans": plans, "replans": replans},
            "qa_perf": {"count": perf_row["c"], "avg_latency_ms": int(avg_ms),
                        "error_rate": error_rate},
            "parse": parse if isinstance(parse, dict) else None,
            "dedup": {"total": dedup_total, "pending": dedup_pending,
                      "covered": dedup.get("covered", 0), "kept": dedup.get("kept", 0),
                      "coexisted": dedup.get("coexisted", 0)},
            "offline": offline}


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
