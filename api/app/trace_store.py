"""Trace 事件流持久化（trace_events）：右侧 Trace 时间线回放的数据源。

- thread_id 即 trace_id（"{conversation_id}:{turn}"，与 chat_threads 一一对应）
- 首次执行与断点续跑的事件经 append_trace 追加进同一行——回放即为「首次+续跑」完整历史
- 只存事件流账本，复用 db.py 的 tx()/query() 通用接口，不改动 db.py
"""
from __future__ import annotations

import json

from . import db

_DDL = """
CREATE TABLE IF NOT EXISTS trace_events (
  thread_id       TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL DEFAULT '',
  turn            INTEGER NOT NULL DEFAULT 0,
  question        TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT 'running',  -- running|done|failed
  events_json     TEXT NOT NULL DEFAULT '[]',
  updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_conv ON trace_events(conversation_id, updated_at);
"""

_inited = False


def init() -> None:
    """建表（幂等）；进程内只执行一次。"""
    global _inited
    if _inited:
        return
    with db.tx() as c:
        c.executescript(_DDL)
    _inited = True


def append_trace(thread_id: str, conversation_id: str, turn: int, question: str,
                 status: str, events: list) -> None:
    """追加事件后落盘（同线程续跑时并入既有历史，回放保持完整时序）；行不存在则创建。"""
    init()
    rows = db.query("SELECT events_json FROM trace_events WHERE thread_id=?", (thread_id,))
    merged = json.loads(rows[0]["events_json"] or "[]") if rows else []
    merged.extend({"kind": k, "data": d} for k, d in events)
    with db.tx() as c:
        c.execute(
            "INSERT INTO trace_events(thread_id,conversation_id,turn,question,status,"
            "events_json,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(thread_id) DO UPDATE SET status=excluded.status,"
            "events_json=excluded.events_json,updated_at=excluded.updated_at",
            (thread_id, conversation_id, turn, question[:500], status,
             json.dumps(merged, ensure_ascii=False), db.now()))


def get_trace(thread_id: str) -> dict | None:
    """回放数据源：{trace_id, question, status, events:[{kind,data}...]} 或 None。"""
    init()
    rows = db.query("SELECT * FROM trace_events WHERE thread_id=?", (thread_id,))
    if not rows:
        return None
    d = dict(rows[0])
    d["trace_id"] = d.pop("thread_id")
    d["events"] = json.loads(d.pop("events_json") or "[]")
    return d
