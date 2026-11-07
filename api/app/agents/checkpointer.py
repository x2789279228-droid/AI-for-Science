"""LangGraph SqliteSaver 断点续跑底座：checkpointer 单例 + 带 checkpoint 的主图单例。

- checkpoint 落盘 data/checkpoints.db（与业务库 scholar.db 分文件），进程重启后仍可恢复
- 单连接 check_same_thread=False：FastAPI 在 daemon 线程里同步 invoke，SqliteSaver
  内部自带 threading.Lock 串行化，跨线程安全
- 子图（executor wrapper 手动 invoke）不挂 checkpointer：避免与父图在同一 thread_id
  上写 checkpoint 冲突；子图粒度的失败由父图节点重跑覆盖（检索幂等）
- 单例创建不加锁：竞态最多多建一个 saver（同文件多连接无害），换取零阻塞风险
"""
from __future__ import annotations

import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from ..config import CHECKPOINT_DB_PATH

_saver: SqliteSaver | None = None
_graph = None  # type: ignore[var-annotated]


def get_saver() -> SqliteSaver:
    global _saver
    if _saver is None:
        conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False, timeout=10.0)
        _saver = SqliteSaver(conn)
        _saver.setup()  # 建表（幂等）
    return _saver


def get_graph():
    """带 checkpointer 的主图进程级单例（run_stream/resume 共用，断点才能续上）。"""
    global _graph
    if _graph is None:
        from .graph import build_graph
        _graph = build_graph(checkpointer=get_saver())
    return _graph
