"""图谱与洞察端点（4.6）：构图+指标+社区+角色 / 社区综述 SSE / Future Work SSE。"""
from __future__ import annotations

import json
import queue as queue_mod
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..agents.insights import classify_citations, community_summary, enrich, future_works
from ..agents.tools import _citation_graph

router = APIRouter(prefix="/api")


def _parse_paper_ids(paper_ids: str | None) -> list[str]:
    if paper_ids:
        return [p.strip() for p in paper_ids.split(",") if p.strip()]
    rows = [r["id"] for r in _db_all_papers()]
    return rows


def _db_all_papers():
    from .. import db
    return db.query("SELECT id FROM papers ORDER BY created_at")


@router.get("/graph")
def get_graph(paper_ids: str | None = None, weighted: bool = True):
    """构图 + 指标 + 社区 + 角色 +（可选）引用含金量加权边。"""
    ids = _parse_paper_ids(paper_ids)
    if not ids:
        raise HTTPException(400, "文献库为空")
    g = enrich(_citation_graph(ids))
    if weighted:
        w = classify_citations(ids)
        weight_map = {f"{e['source']}|{e['target']}": e for e in w["edges"]}
        g["edges"] = [weight_map.get(f"{e['source']}|{e['target']}",
                                     {"source": e["source"], "target": e["target"],
                                      "weight": 0.0, "examples": []}) for e in g["edges"]]
        g["class_stat"] = w["class_stat"]
        g["mention_count"] = w["mention_count"]
    # insights：基石/边缘角色汇总
    g["insights"] = {
        "cornerstone": [n for n in g["nodes"] if n.get("role") == "cornerstone"],
        "peripheral": [n for n in g["nodes"] if n.get("role") == "peripheral"],
    }
    return g


class SummaryBody(BaseModel):
    paper_ids: list[str]
    community_id: int


@router.post("/graph/summary")
def graph_summary(body: SummaryBody):
    """社区综述 SSE：status → delta（流式）→ final（含 claims/anchors，逐论断校验过）。"""
    q: queue_mod.Queue = queue_mod.Queue()

    def worker() -> None:
        try:
            q.put(("status", {"stage": "evidence", "note": "收集社区代表论文证据…"}))
            out = community_summary(body.paper_ids, body.community_id)
            if out.get("error"):
                q.put(("error", {"message": out["error"]}))
                return
            for piece in _stream_by_sentence(out["summary"]):
                q.put(("delta", {"text": piece}))
            q.put(("final", {"summary": out["summary"], "claims": out["claims"],
                             "anchors": out["anchors"], "guardrails": out["guardrails"]}))
        except Exception as e:  # noqa: BLE001
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            q.put(("done", {}))

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            try:
                kind, data = q.get(timeout=300)
            except queue_mod.Empty:
                yield 'event: error\ndata: {"message":"summary timeout"}\n\n'
                return
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if kind == "done":
                return

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _stream_by_sentence(text: str):
    """按句切分模拟流式（综述是整段生成，切句下发保证打字机体验）。"""
    import re
    for piece in re.split(r"(?<=[。！？.!?])\s*", text):
        if piece.strip():
            yield piece.strip()


class IdeasBody(BaseModel):
    paper_ids: list[str]


@router.post("/ideas")
def ideas(body: IdeasBody):
    """Future Work 去重过滤 SSE：status → final {solved, open, total}。"""
    if not body.paper_ids:
        raise HTTPException(400, "paper_ids 为空")
    q: queue_mod.Queue = queue_mod.Queue()

    def worker() -> None:
        try:
            q.put(("status", {"stage": "extract", "note": "扫描 Conclusion/Limitation 抽取想法…"}))
            out = future_works(body.paper_ids)
            q.put(("status", {"stage": "judge",
                              "note": f"共 {out['total']} 条想法，判定完成"}))
            q.put(("final", {"solved": out["solved"], "open": out["open"],
                             "total": out["total"]}))
        except Exception as e:  # noqa: BLE001
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            q.put(("done", {}))

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            try:
                kind, data = q.get(timeout=600)
            except queue_mod.Empty:
                yield 'event: error\ndata: {"message":"ideas timeout"}\n\n'
                return
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if kind == "done":
                return

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
