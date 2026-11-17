"""断点续跑路由：会话线程查询 + 失败恢复执行（SSE，协议与 /api/chat 一致）+ 会话管理。"""
from __future__ import annotations

import json
import queue as queue_mod
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import threads_store, trace_store
from ..agents import cancel, runner

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


# ---------- 停止生成（协作式取消） ----------

@router.post("/chat/{thread_id}/stop")
def stop_chat(thread_id: str):
    """停止生成：置位取消标志，responder 流式循环逐 piece 检查后中断，
    以已生成部分收尾（guardrail=user_stopped）。幂等；线程不存在 404。"""
    if threads_store.get_thread(thread_id) is None:
        raise HTTPException(404, "线程不存在")
    return {"stopped": cancel.cancel(thread_id)}


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
    cancel.register(thread_id)   # 续跑同样可被停止
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


# ---------- 会话管理（ChatGLM 式会话侧栏数据源） ----------

@router.get("/conversations")
def list_conversations(limit: int = 50, doc_mode: str = ""):
    """会话列表：轮数/最后活跃/标题（自定义标题优先，首问兜底），按最后活跃倒序。
    doc_mode=paper|general 按模式过滤（legacy 空标记历史两种模式均可见）；空 = 全量。"""
    return {"conversations": threads_store.list_conversations(limit, doc_mode.strip())}


@router.get("/conversations/{conversation_id}/turns")
def conversation_turns(conversation_id: str):
    """会话内全部轮次（turn 升序）；完整 final 元数据由前端按 thread_id 调 /qa/{trace_id} 还原。"""
    return {"turns": threads_store.conversation_turns(conversation_id)}


class _RenameBody(BaseModel):
    title: str


@router.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, body: _RenameBody):
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    threads_store.set_title(conversation_id, title)
    return {"ok": True, "title": title[:80]}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
    """删除会话：线程账本+标题行硬删；回放账本随删，checkpoint 逐线程 best-effort 清（孤儿无害）。"""
    thread_ids = [r["thread_id"] for r in threads_store.list_threads(conversation_id, limit=500)]
    n = threads_store.delete_conversation(conversation_id)
    if n == 0:
        raise HTTPException(404, "会话不存在")
    trace_store.delete_traces_by_conversation(conversation_id)
    for tid in thread_ids:
        try:
            from ..agents.checkpointer import get_saver
            get_saver().delete_thread(tid)
        except Exception:  # noqa: BLE001, S110 —— checkpoint 清理失败不阻塞删除（孤儿无害）
            pass
    return {"ok": True, "removed": n}
