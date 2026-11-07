"""REST + SSE 路由（方案 §8 MVP 子集）。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import db, jobs
from ..config import settings
from ..llm import client as llm
from ..rag import qa as qa_mod

router = APIRouter(prefix="/api")


# ---------- papers ----------

@router.post("/papers")
async def upload_paper(file: UploadFile = File(...)):
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
    out = []
    for r in rows:
        d = dict(r)
        d["parsed"] = bool(r["docir_path"])
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


@router.get("/papers/{paper_id}/docir")
def get_docir(paper_id: str, page: int | None = None):
    rows = db.query("SELECT docir_path FROM papers WHERE id=?", (paper_id,))
    if not rows or not rows[0]["docir_path"]:
        raise HTTPException(404, "未解析或不存在")
    data = json.loads(Path(rows[0]["docir_path"]).read_text(encoding="utf-8"))
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


@router.post("/chat")
def chat(body: ChatBody):
    paper_ids = body.paper_ids or [r["id"] for r in db.query("SELECT id FROM papers")]
    if not paper_ids:
        raise HTTPException(400, "文献库为空，请先上传 PDF")

    def sse():
        for ev in qa_mod.answer_stream(body.question.strip(), paper_ids):
            payload = json.dumps(ev["data"], ensure_ascii=False)
            yield f"event: {ev['event']}\ndata: {payload}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- 透明度中心 ----------

@router.get("/audit/prompts")
def audit_prompts(limit: int = 50):
    rows = db.query("SELECT * FROM qa_logs ORDER BY ts DESC LIMIT ?", (min(limit, 200),))
    return {"logs": [dict(r) for r in rows],
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
