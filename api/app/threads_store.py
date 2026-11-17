"""会话线程登记表（chat_threads）：断点续跑的状态账本。

- thread_id = "{conversation_id}#{turn}"：会话内每轮独立（state 的 trace/raw_hits 等为
  operator.add 累加键，同 thread 连跑两轮会跨轮污染且无法清除），conversation_id 保证归属
- 只登记"账"，图状态本体存在 LangGraph checkpoints.db（见 agents/checkpointer.py）
- 复用 db.py 的 tx()/query() 通用接口，不改动 db.py
"""
from __future__ import annotations

import json

from . import db

MAX_RESUME_ATTEMPTS = 3  # 自动恢复尝试上限（含启动恢复与手动恢复），防死循环

_DDL = """
CREATE TABLE IF NOT EXISTS chat_threads (
  thread_id       TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn            INTEGER NOT NULL,
  question        TEXT NOT NULL,
  paper_ids_json  TEXT NOT NULL DEFAULT '[]',
  status          TEXT NOT NULL DEFAULT 'running',  -- running|done|failed
  error           TEXT DEFAULT '',
  answer          TEXT DEFAULT '',
  doc_mode        TEXT NOT NULL DEFAULT '',         -- paper|general|''(legacy：两模式均可见)
  resume_attempts INTEGER NOT NULL DEFAULT 0,
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_conv ON chat_threads(conversation_id, turn);
CREATE INDEX IF NOT EXISTS idx_threads_status ON chat_threads(status);
"""

# 会话标题表（ChatGLM 式会话侧栏的重命名）：核心账本 chat_threads 不动，标题可选覆盖
_CONV_DDL = """
CREATE TABLE IF NOT EXISTS chat_conversations (
  conversation_id TEXT PRIMARY KEY,
  title           TEXT NOT NULL DEFAULT '',
  created_at      REAL NOT NULL
);
"""

_inited = False


def init() -> None:
    """建表（幂等）；进程内只执行一次。含旧库 doc_mode 列迁移。"""
    global _inited
    if _inited:
        return
    with db.tx() as c:
        c.executescript(_DDL)
        c.executescript(_CONV_DDL)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(chat_threads)").fetchall()}
        if "doc_mode" not in cols:   # 旧库迁移：补列（已有行留空 = legacy，两模式均可见）
            c.execute("ALTER TABLE chat_threads ADD COLUMN doc_mode TEXT NOT NULL DEFAULT ''")
    _inited = True


def next_turn(conversation_id: str) -> int:
    init()
    row = db.query("SELECT COALESCE(MAX(turn),0) AS t FROM chat_threads WHERE conversation_id=?",
                   (conversation_id,))
    return int(row[0]["t"]) + 1


def make_thread_id(conversation_id: str, turn: int) -> str:
    # ":" 为 URL 安全分隔符（thread_id 会出现在路径参数中）
    return f"{conversation_id}:{turn}"


def start_thread(thread_id: str, conversation_id: str, turn: int, question: str,
                 paper_ids: list[str], doc_mode: str = "") -> None:
    init()
    with db.tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO chat_threads(thread_id,conversation_id,turn,question,"
            "paper_ids_json,status,doc_mode,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'running',?,?,?)",
            (thread_id, conversation_id, turn, question,
             json.dumps(paper_ids, ensure_ascii=False), doc_mode, db.now(), db.now()))


def finish_thread(thread_id: str, status: str, error: str = "", answer: str = "") -> None:
    init()
    with db.tx() as c:
        c.execute("UPDATE chat_threads SET status=?,error=?,answer=?,updated_at=? WHERE thread_id=?",
                  (status, error[:500], answer[:8000], db.now(), thread_id))


def mark_resume(thread_id: str) -> bool:
    """进入恢复执行：status→running 且 attempts+1；超出上限返回 False。"""
    init()
    with db.tx() as c:
        row = c.execute("SELECT resume_attempts FROM chat_threads WHERE thread_id=?",
                        (thread_id,)).fetchone()
        if row is None or row["resume_attempts"] >= MAX_RESUME_ATTEMPTS:
            return False
        c.execute("UPDATE chat_threads SET status='running',resume_attempts=resume_attempts+1,"
                  "updated_at=? WHERE thread_id=?", (db.now(), thread_id))
    return True


def get_thread(thread_id: str) -> dict | None:
    init()
    rows = db.query("SELECT * FROM chat_threads WHERE thread_id=?", (thread_id,))
    return _decode(rows[0]) if rows else None


def list_threads(conversation_id: str = "", status: str = "", limit: int = 50) -> list[dict]:
    init()
    sql = "SELECT * FROM chat_threads WHERE 1=1"
    params: list = []
    if conversation_id:
        sql += " AND conversation_id=?"
        params.append(conversation_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(min(limit, 200))
    return [_decode(r) for r in db.query(sql, tuple(params))]


def pending_recovery() -> list[dict]:
    """启动恢复扫描：崩溃遗留的 running + 失败的 failed，且未超出恢复预算。"""
    init()
    rows = db.query(
        "SELECT * FROM chat_threads WHERE status IN ('running','failed')"
        " AND resume_attempts < ? ORDER BY updated_at ASC", (MAX_RESUME_ATTEMPTS,))
    return [_decode(r) for r in rows]


# ---------- 会话管理（ChatGLM 式会话侧栏数据源；会话=conversation_id） ----------

def set_title(conversation_id: str, title: str) -> None:
    """重命名会话（无则建标题行）；首问兜底由 list_conversations 的 COALESCE 承担。"""
    init()
    with db.tx() as c:
        c.execute(
            "INSERT INTO chat_conversations(conversation_id,title,created_at) VALUES(?,?,?) "
            "ON CONFLICT(conversation_id) DO UPDATE SET title=excluded.title",
            (conversation_id, title[:80], db.now()))


def list_conversations(limit: int = 50, doc_mode: str = "") -> list[dict]:
    """会话列表：chat_threads 按 conversation_id 聚合（轮数/最后活跃/首末问），
    自定义标题优先于首问，截 40 字。按最后活跃倒序。
    doc_mode 过滤：paper|general 只看对应模式；legacy 空标记行两种模式均可见
    （迁移前历史不消失）。空串 = 不过滤（全量）。"""
    init()
    sql = (
        "SELECT t.conversation_id AS id,"
        "       COUNT(*) AS turns,"
        "       MAX(t.updated_at) AS updated_at,"
        "       (SELECT question FROM chat_threads x WHERE x.conversation_id=t.conversation_id"
        "         ORDER BY turn ASC LIMIT 1) AS first_question,"
        "       (SELECT question FROM chat_threads x WHERE x.conversation_id=t.conversation_id"
        "         ORDER BY turn DESC LIMIT 1) AS last_question,"
        "       COALESCE((SELECT title FROM chat_conversations c"
        "         WHERE c.conversation_id=t.conversation_id),'') AS custom_title"
        " FROM chat_threads t WHERE 1=1"
    )
    params: list = []
    if doc_mode in ("paper", "general"):
        sql += " AND (t.doc_mode=? OR t.doc_mode='')"
        params.append(doc_mode)
    sql += " GROUP BY t.conversation_id ORDER BY updated_at DESC LIMIT ?"
    params.append(min(limit, 200))
    out = []
    for r in db.query(sql, tuple(params)):
        d = dict(r)
        d["title"] = (d.pop("custom_title") or d.pop("first_question") or "新对话")[:40]
        d.pop("first_question", None)
        out.append(d)
    return out


def conversation_turns(conversation_id: str, limit: int = 100) -> list[dict]:
    """会话内全部轮次（turn 升序）：前端侧栏点开会话还原历史。"""
    init()
    rows = db.query(
        "SELECT thread_id,turn,question,answer,status,created_at FROM chat_threads "
        "WHERE conversation_id=? ORDER BY turn ASC LIMIT ?",
        (conversation_id, min(limit, 500)))
    return [dict(r) for r in rows]


def delete_conversation(conversation_id: str) -> int:
    """删除会话：全部线程行 + 标题行；返回删除线程数（trace/checkpoint 由调用方 best-effort 清）。"""
    init()
    with db.tx() as c:
        cur = c.execute("DELETE FROM chat_threads WHERE conversation_id=?", (conversation_id,))
        c.execute("DELETE FROM chat_conversations WHERE conversation_id=?", (conversation_id,))
        return cur.rowcount


def _decode(row) -> dict:
    d = dict(row)
    d["paper_ids"] = json.loads(d.pop("paper_ids_json") or "[]")
    return d
