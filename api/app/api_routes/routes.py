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


# ---------- papers ----------

@router.post("/papers")
async def upload_paper(file: UploadFile = File(...)):  # noqa: B008 —— FastAPI 惯用法
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "仅支持 PDF")
    content = await file.read()
    if len(content) > 200 * 1024 * 1024:
        raise HTTPException(413, "文件超过 200MB")
    result = jobs.upload_pdf(file.filename, content)
    return result


@router.get("/papers")
def list_papers():
    rows = db.query("SELECT id,title,arxiv_id,page_count,version,supersedes,created_at,docir_path"
                    " FROM papers ORDER BY created_at DESC")
    # 去重预警 map（P0-1：前端黄色预警条数据源；每篇取最新一条 dedup_alert payload）
    alerts: dict[str, dict] = {}
    for j in db.query("SELECT paper_id, payload FROM jobs WHERE kind='dedup_alert' "
                      "ORDER BY created_at DESC"):
        try:   # 坏 payload 不拖垮整个列表（P2-1）
            alerts.setdefault(j["paper_id"], json.loads(j["payload"] or "{}"))
        except ValueError:
            continue
    out = []
    for r in rows:
        d = dict(r)
        d["parsed"] = bool(r["docir_path"])
        if r["id"] in alerts:
            d["dedup_alert"] = alerts[r["id"]]
        out.append(d)
    return {"papers": out}


@router.get("/papers/{paper_id}")
def get_paper(paper_id: str):
    rows = db.query("SELECT * FROM papers WHERE id=?", (paper_id,))
    if not rows:
        raise HTTPException(404, "not found")
    p = dict(rows[0])
    p.pop("meta_json", None)
    refs = db.query('SELECT idx,title,arxiv_id,doi FROM "references" WHERE paper_id=? ORDER BY idx',
                    (paper_id,))
    p["references_count"] = len(refs)
    # 去重预警（官方用例 1）
    alert = db.query("SELECT payload FROM jobs WHERE paper_id=? AND kind='dedup_alert'"
                     " ORDER BY created_at DESC LIMIT 1", (paper_id,))
    if alert:
        p["dedup_alert"] = json.loads(alert[0]["payload"])
    return p


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


# ---------- jobs ----------

@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = db.get_job(job_id)
    if not job:
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

    paper_ids = body.paper_ids or [r["id"] for r in db.query("SELECT id FROM papers")]
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

@router.get("/qa/{trace_id}")
def get_qa_trace(trace_id: str):
    """回放一次问答的完整事件流（trace_id = thread_id，含断点续跑并入的事件）。"""
    trace = trace_store.get_trace(trace_id)
    if trace is None:
        raise HTTPException(404, "trace 不存在")
    return trace
