"""SSE 桥：在工作线程里 invoke 状态图，节点卡片/流式增量经线程安全队列交给路由层。

事件序列（协议向后兼容，trace 为新增）：
  trace*(节点卡，live) → delta*(仅生成式) → retrieval → final(含 trace 全量) → done
"""
from __future__ import annotations

import queue as queue_mod
import time

from .. import db
from ..config import settings
from ..rag.answering import hit_summary
from .graph import build_graph
from .nodes import graph_path


def run_stream(question: str, paper_ids: list[str], q: queue_mod.Queue) -> None:
    """阻塞执行完整状态图，事件逐条入队；最后一条必为 done。"""
    graph = build_graph()
    config = {"recursion_limit": 40, "configurable": {"queue": q}}  # 恢复循环最坏 ≈17 超步，留余量
    t0 = time.time()
    try:
        final = graph.invoke(
            {"question": question, "paper_ids": paper_ids, "retries": 0}, config)
    except Exception as e:  # noqa: BLE001 —— 图级故障也必须以 done 收尾
        q.put(("final", {"answer": f"[执行失败：{type(e).__name__}: {e}]",
                         "claims": [], "mode": "none",
                         "guardrails": [f"graph_error:{type(e).__name__}"], "trace": []}))
        q.put(("done", {}))
        return

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
                     "elapsed": round(time.time() - t0, 1)}))
    q.put(("done", {"temperature": settings.llm_temperature}))
