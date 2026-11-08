"""SSE 桥：在工作线程里 invoke 状态图，节点卡片/流式增量经线程安全队列交给路由层。

事件序列（协议向后兼容，trace 为新增）：
  trace*(节点卡，live) → delta*(仅生成式) → retrieval → final(含 trace 全量) → done
  协议 v1.1 增量：plan / tool_call / tool_result / evidence / verifier（Trace 时间线）。
事件流经 _TeeQueue 记账落盘 trace_events——thread_id 即 trace_id，GET /api/qa/{trace_id} 回放。

断点续跑（SqliteSaver）：每次执行携带 thread_id（"{conversation_id}#{turn}"），checkpoint
落盘 checkpoints.db；图级异常时线程标记 failed，resume_stream() 以 invoke(None) 从最近
checkpoint 续跑——已完成节点不重跑，累加键（trace/raw_hits 等）从 checkpoint 恢复不重复。
"""
from __future__ import annotations

import queue as queue_mod
import time
import uuid

from .. import db, threads_store, trace_store
from ..config import settings
from ..rag.answering import hit_summary
from .checkpointer import get_graph
from .nodes import graph_path


class _TeeQueue:
    """事件记账代理：put 先落本地列表（落盘 trace_events 用）再转发真实队列。

    节点侧只调用 q.put（鸭子类型），经 executor wrapper 的 configurable 白名单透传后
    行为与原 Queue 一致；capture 的 (kind, data) 序列即该线程的完整回放数据。
    """

    def __init__(self, sink: queue_mod.Queue) -> None:
        self._sink = sink
        self.captured: list[tuple] = []

    def put(self, item) -> None:
        self.captured.append(item)
        self._sink.put(item)

    def record_only(self, item) -> None:
        """只记账不转发（P1-1：done 先记账、trace 落盘后再由调用方转发，消回放竞态）。"""
        self.captured.append(item)


def _save_trace(tee: _TeeQueue, thread_id: str, conversation_id: str | None,
                turn: int | None, question: str, status: str) -> None:
    try:
        trace_store.append_trace(thread_id, conversation_id or "", turn or 0,
                                 question, status, tee.captured)
    except Exception:  # noqa: BLE001, S110 —— trace 落盘失败不阻塞主链路
        pass


def run_stream(question: str, paper_ids: list[str], q: queue_mod.Queue, *,
               thread_id: str | None = None, conversation_id: str | None = None,
               turn: int | None = None, forced_agent: str = "auto") -> None:
    """阻塞执行完整状态图，事件逐条入队；最后一条必为 done。

    thread_id/conversation_id/turn 缺省时自动生成（conversation_id → next_turn 推导），
    向后兼容旧的三位置参数调用。forced_agent 为 UI 强制指定的 Agent（auto/reader/…）。
    事件流经 _TeeQueue 记账并落盘 trace_events（回放数据源）。
    """
    if thread_id is None:
        conversation_id = conversation_id or uuid.uuid4().hex
        turn = turn if turn is not None else threads_store.next_turn(conversation_id)
        thread_id = threads_store.make_thread_id(conversation_id, turn)
    threads_store.start_thread(thread_id, conversation_id or "", turn or 0,
                               question, paper_ids)

    tee = _TeeQueue(q)
    graph = get_graph()
    config = {"recursion_limit": 40, "configurable": {"thread_id": thread_id, "queue": tee}}
    t0 = time.time()
    try:
        final = graph.invoke(
            {"question": question, "paper_ids": paper_ids, "retries": 0,
             "forced_agent": forced_agent}, config)
    except Exception as e:  # noqa: BLE001 —— 图级故障也必须以 done 收尾；断点已落盘可恢复
        threads_store.finish_thread(thread_id, "failed", error=f"{type(e).__name__}: {e}")
        tee.put(("final", {"answer": f"[执行失败：{type(e).__name__}: {e}]",
                           "claims": [], "mode": "none",
                           "guardrails": [f"graph_error:{type(e).__name__}"], "trace": [],
                           "thread_id": thread_id, "conversation_id": conversation_id,
                           "resumable": True}))
        tee.put(("done", {}))
        _save_trace(tee, thread_id, conversation_id, turn, question, "failed")
        return

    threads_store.finish_thread(thread_id, "done", answer=final.get("answer", ""))
    _push_success(tee, final, question, t0, thread_id, conversation_id, defer_done=True)
    _save_trace(tee, thread_id, conversation_id, turn, question, "done")
    tee.put(("done", {"temperature": settings.llm_temperature}))


def resume_stream(thread_id: str, q: queue_mod.Queue) -> None:
    """从最近 checkpoint 续跑指定线程（graph.invoke(None)），事件协议与 run_stream 一致。"""
    row = threads_store.get_thread(thread_id)
    if row is None:
        q.put(("final", {"answer": "[恢复失败：线程不存在]", "claims": [], "mode": "none",
                         "guardrails": ["resume_not_found"], "trace": [],
                         "thread_id": thread_id, "resumable": False}))
        q.put(("done", {}))
        return
    if row["status"] == "done":
        q.put(("final", {"answer": row.get("answer") or "[该轮已完成，无需恢复]",
                         "claims": [], "mode": "none",
                         "guardrails": ["resume_already_done"], "trace": [],
                         "thread_id": thread_id, "resumable": False}))
        q.put(("done", {}))
        return
    if not threads_store.mark_resume(thread_id):
        q.put(("final", {"answer": f"[恢复失败：超出恢复预算 {threads_store.MAX_RESUME_ATTEMPTS} 次]",
                         "claims": [], "mode": "none",
                         "guardrails": ["resume_exhausted"], "trace": [],
                         "thread_id": thread_id, "resumable": False}))
        q.put(("done", {}))
        return

    tee = _TeeQueue(q)
    graph = get_graph()
    config = {"recursion_limit": 40, "configurable": {"thread_id": thread_id, "queue": tee}}
    t0 = time.time()
    try:
        final = graph.invoke(None, config)  # None → 从最近 checkpoint 的断点继续
    except Exception as e:  # noqa: BLE001
        threads_store.finish_thread(thread_id, "failed", error=f"{type(e).__name__}: {e}")
        attempts = threads_store.get_thread(thread_id)["resume_attempts"]
        tee.put(("final", {"answer": f"[执行失败：{type(e).__name__}: {e}]",
                           "claims": [], "mode": "none",
                           "guardrails": [f"graph_error:{type(e).__name__}"], "trace": [],
                           "thread_id": thread_id,
                           "conversation_id": row["conversation_id"],
                           "resumable": attempts < threads_store.MAX_RESUME_ATTEMPTS}))
        tee.put(("done", {}))
        _save_trace(tee, thread_id, row["conversation_id"], row["turn"],
                    row["question"], "failed")
        return

    threads_store.finish_thread(thread_id, "done", answer=final.get("answer", ""))
    _push_success(tee, final, row["question"], t0, thread_id, row["conversation_id"],
                  defer_done=True)
    _save_trace(tee, thread_id, row["conversation_id"], row["turn"],
                row["question"], "done")
    tee.put(("done", {"temperature": settings.llm_temperature}))


def _push_success(q, final: dict, question: str, t0: float,
                  thread_id: str, conversation_id: str | None,
                  defer_done: bool = False) -> None:
    """成功收尾事件：retrieval → log_qa → final → done（run/resume 共用）。

    q 实参为 _TeeQueue：收尾事件经它转发真实队列的同时同步入账，保证回放完整。
    defer_done=True 时 done 只记账不转发——调用方须在 _save_trace 之后转发 done，
    保证「收到 done → 立即回放」必得完整 trace（P1-1 回放竞态）。
    """
    branch_stats = final.get("branch_stats", [])
    q.put(("retrieval", {
        "channels": sorted({c for b in branch_stats for c in b.get("channels", [])}),
        "bm25_count": sum(b.get("bm25", 0) for b in branch_stats),
        "dense_count": sum(b.get("dense", 0) for b in branch_stats),
        "hits": hit_summary(final.get("evidence", [])),
    }))

    trace = final.get("trace", [])
    guardrails = sorted(set(final.get("guardrails", [])))
    db.log_qa(module="qa", question=question,
              answer=final.get("answer", "")[:8000],
              latency_ms=int((time.time() - t0) * 1000),
              guardrail=(f"graph:{graph_path(trace)};" + ";".join(guardrails))[:400])
    q.put(("final", {"answer": final.get("answer", ""),
                     "claims": final.get("claims", []),
                     "mode": final.get("mode", "none"),
                     "guardrails": guardrails,
                     "trace": trace,
                     "elapsed": round(time.time() - t0, 1),
                     "agent": final.get("agent", "reader"),
                     "handoffs": final.get("handoffs", []),
                     "anchors": final.get("anchors", {}),   # 恒在：溯源链路字段稳定（空=抽取式无锚点）
                     **({"compare_table": final["compare_table"]}
                        if final.get("compare_table") else {}),
                     **({"graph_data": final["graph_data"]}
                        if final.get("graph_data") else {}),
                     **({"outline": final["outline"]}
                        if final.get("outline") else {}),
                     **({"chart_spec": final["chart_spec"]}
                        if final.get("chart_spec") else {}),
                     "thread_id": thread_id, "conversation_id": conversation_id}))
    done_evt = ("done", {"temperature": settings.llm_temperature})
    if defer_done:
        q.record_only(done_evt)
    else:
        q.put(done_evt)
