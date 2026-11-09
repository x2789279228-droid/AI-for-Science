"""图谱与洞察端点（4.6）：构图+指标+社区+角色 / 社区综述 SSE / Future Work SSE。"""
from __future__ import annotations

import json
import queue as queue_mod
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..agents.insights import (
    _idea_sig,
    classify_citations,
    community_summary,
    enrich,
    future_checks,
    future_works,
    reference_quality,
    survey,
)
from ..agents.tools import _citation_graph

router = APIRouter(prefix="/api")


@router.get("/papers/{paper_id}/ref-quality")
def get_ref_quality(paper_id: str):
    """单篇参考文献含金量（模块四 P0-2）：References 面板懒加载 + 可逆过滤依据。"""
    from .. import db

    if not db.query("SELECT id FROM papers WHERE id=?", (paper_id,)):
        raise HTTPException(404, "论文不存在")
    return reference_quality(paper_id)


def _parse_paper_ids(paper_ids: str | None) -> list[str]:
    if paper_ids:
        return [p.strip() for p in paper_ids.split(",") if p.strip()]
    rows = [r["id"] for r in _db_all_papers()]
    return rows


def _db_all_papers():
    from .. import db
    return db.query("SELECT id FROM papers WHERE status='active' ORDER BY created_at")


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


class SurveyBody(BaseModel):
    paper_ids: list[str]
    refresh: bool = False     # 命中缓存时是否强制重新生成


@router.post("/graph/survey")
def graph_survey(body: SurveyBody):
    """整批领域综述 SSE（模块五 P0-2）：五节结构，每节 status → delta → final。
    结果落 survey_reports（paper_ids_sig 缓存键），二次请求秒回缓存。"""
    if len(body.paper_ids) < 2:
        raise HTTPException(400, "综述至少选择 2 篇论文")
    if len(body.paper_ids) > 50:
        raise HTTPException(400, "单批上限 50 篇")
    sig = ",".join(sorted(body.paper_ids))
    from .. import db as _db
    if not body.refresh:
        hit = _db.query("SELECT * FROM survey_reports WHERE paper_ids_sig=? "
                        "ORDER BY created_at DESC LIMIT 1", (sig,))
        if hit and hit[0]["sections"]:
            import json as _json
            row = hit[0]
            sections = _json.loads(row["sections"])
            stats = _json.loads(row["stats"] or "{}")

            def cached():
                yield ('event: status\ndata: {"stage":"cache","note":"命中缓存"}\n\n')
                for s in sections:
                    for piece in _stream_by_sentence(s["content"]):
                        yield f'event: delta\ndata: {_json.dumps({"section": s["title"], "text": piece}, ensure_ascii=False)}\n\n'
                yield (f'event: final\ndata: {_json.dumps({"sections": sections, "stats": stats, "cached": True}, ensure_ascii=False)}\n\n')
                yield 'event: done\ndata: {}\n\n'
            from fastapi.responses import StreamingResponse as _SR
            return _SR(cached(), media_type="text/event-stream",
                       headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    q: queue_mod.Queue = queue_mod.Queue()

    def worker() -> None:
        import json as _json
        import time as _time
        try:
            def prog(name: str) -> None:
                q.put(("status", {"stage": "section", "note": f"生成小节：{name}"}))

            out = survey(body.paper_ids, progress=prog)
            if out.get("error"):
                q.put(("error", {"message": out["error"]}))
                return
            for s in out["sections"]:
                for piece in _stream_by_sentence(s["content"]):
                    q.put(("delta", {"section": s["title"], "text": piece}))
            q.put(("final", {"sections": out["sections"], "stats": out["stats"],
                             "guardrails": out.get("guardrails", [])}))
            with _db.tx() as c:
                c.execute("INSERT INTO survey_reports(id,paper_ids_sig,stats,content_md,"
                          "sections,created_at) VALUES(?,?,?,?,?,?)",
                          (_db.new_id("sr_"), sig, _json.dumps(out["stats"], ensure_ascii=False),
                           out.get("content_md", ""), _json.dumps(out["sections"], ensure_ascii=False),
                           _time.time()))
        except Exception as e:  # noqa: BLE001
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            q.put(("done", {}))

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            try:
                kind, data = q.get(timeout=900)
            except queue_mod.Empty:
                yield 'event: error\ndata: {"message":"survey timeout"}\n\n'
                return
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if kind == "done":
                return

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/future-checks")
def future_checks_route(body: IdeasBody):
    """外部最新进展核查 SSE（模块五 P0-3）：抽取去重 → S2 外部证据 → 判定落库。
    verdict ∈ solved|partial|open|open_unverified（无外部证据=未验证，不冒充已排除）。"""
    if not body.paper_ids:
        raise HTTPException(400, "paper_ids 为空")
    q: queue_mod.Queue = queue_mod.Queue()

    def worker() -> None:
        import json as _json
        import time as _time
        try:
            def prog(note: str) -> None:
                q.put(("status", {"stage": "check", "note": note}))

            items = future_checks(body.paper_ids, progress=prog)
            now = _time.time()
            from .. import db as _db
            with _db.tx() as c:
                for it in items:
                    c.execute("INSERT OR REPLACE INTO future_checks(id,idea_sig,idea_text,"
                              "source_paper,verdict,confidence,evidence,external_count,checked_at)"
                              " VALUES(?,?,?,?,?,?,?,?,?)",
                              (_db.new_id("fc_"), _idea_sig(it["idea_text"]),
                               it["idea_text"][:400], it.get("source_paper", ""),
                               it["verdict"], it.get("confidence", 0.0),
                               _json.dumps(it.get("external", []), ensure_ascii=False),
                               len(it.get("external", [])), now))
            q.put(("final", {"items": items, "total": len(items)}))
        except Exception as e:  # noqa: BLE001
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            q.put(("done", {}))

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            try:
                kind, data = q.get(timeout=900)
            except queue_mod.Empty:
                yield 'event: error\ndata: {"message":"future-checks timeout"}\n\n'
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
