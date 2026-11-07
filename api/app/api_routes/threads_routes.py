"""断点续跑路由：会话线程查询 + 失败恢复执行（SSE，协议与 /api/chat 一致）。"""
from __future__ import annotations

import json
import queue as queue_mod
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import threads_store
from ..agents import runner

router = APIRouter(prefix="/api")


# ---------- 线程查询 ----------

@router.get("/threads")
def list_threads(conversation_id: str = "", status: str = "", limit: int = 50):
    return {"threads": threads_store.list_threads(conversation_id.strip(), status.strip(), limit)}


@router.get("/threads/{thread_id}")
def get_thread(thread_id: str):
    row = threads_store.get_thread(thread_id)
    if row is None:
        raise HTTPException(404, "not found")
    # checkpoint 视角：崩溃/失败时下一个待执行节点（断点位置）
    row["pending"] = _pending_nodes(thread_id)
    return row


def _pending_nodes(thread_id: str) -> list[str]:
    try:
        snap = runner.get_graph().get_state({"configurable": {"thread_id": thread_id}})
        return [str(n) for n in (snap.next or ())]
    except Exception:  # noqa: BLE001 —— 状态查询失败不阻塞详情返回
        return []


# ---------- 恢复执行（SSE） ----------

@router.post("/chat/{thread_id}/resume")
def resume_chat(thread_id: str):
    row = threads_store.get_thread(thread_id)
    if row is None:
        raise HTTPException(404, "not found")
    if row["status"] == "done":
        raise HTTPException(409, "该轮已完成，无需恢复")
    if row["status"] == "running":
        raise HTTPException(409, "该轮仍在执行中（崩溃遗留线程由服务启动时自动恢复）")

    q: queue_mod.Queue = queue_mod.Queue()
    threading.Thread(target=runner.resume_stream, args=(thread_id, q), daemon=True).start()

    def sse():
        while True:
            try:
                kind, data = q.get(timeout=300)
            except queue_mod.Empty:
                yield ('event: final\ndata: {"answer":"[执行超时]","claims":[],"mode":"none",'
                       '"guardrails":["timeout"],"trace":[]}\n\n')
                yield "event: done\ndata: {}\n\n"
                return
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if kind == "done":
                return

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
