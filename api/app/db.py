"""SQLite 单文件数据底座：papers / chunks(含FTS5) / jobs / qa_logs / references。

按参赛方案 §4.3 设计，FTS5 作为 Sparse/BM25 检索通道。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .config import DB_PATH

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL DEFAULT '',
  arxiv_id    TEXT DEFAULT '',
  file_path   TEXT NOT NULL,
  file_hash   TEXT NOT NULL,
  page_count  INTEGER DEFAULT 0,
  docir_path  TEXT DEFAULT '',
  version     TEXT DEFAULT '',
  supersedes  TEXT DEFAULT '',
  created_at  REAL NOT NULL,
  meta_json   TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS chunks (
  id           TEXT PRIMARY KEY,
  paper_id     TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  text         TEXT NOT NULL,
  text_cjk     TEXT DEFAULT '',         -- CJK bigram 预切（FTS 中文命中，见 rag/cjk.py）
  page         INTEGER,
  bbox_json    TEXT DEFAULT '[]',
  section_path TEXT DEFAULT '',
  block_ids    TEXT DEFAULT '[]',
  kind         TEXT DEFAULT 'detail',   -- detail | summary | table
  vec          BLOB                     -- 索引时计算的 dense 向量（float32 LE）
);
CREATE INDEX IF NOT EXISTS idx_chunks_paper ON chunks(paper_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED, paper_id UNINDEXED, text_cjk, tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS "references" (
  id        TEXT PRIMARY KEY,
  paper_id  TEXT NOT NULL,
  idx       INTEGER,
  raw       TEXT,
  title     TEXT DEFAULT '',
  arxiv_id  TEXT DEFAULT '',
  doi       TEXT DEFAULT ''
);

-- 4.7 翻译：全局术语表（同一术语全文译法一致）
CREATE TABLE IF NOT EXISTS terminology (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  term        TEXT NOT NULL,
  translation TEXT NOT NULL,
  source      TEXT DEFAULT 'manual',   -- manual | selection
  created_at  REAL NOT NULL,
  UNIQUE(term)
);

-- 4.7 翻译：block 级译文缓存（src_hash 命中即跳过 LLM → 可中断续译）
CREATE TABLE IF NOT EXISTS translations (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  paper_id   TEXT NOT NULL,
  block_id   TEXT NOT NULL,
  lang       TEXT NOT NULL DEFAULT 'zh',
  text       TEXT NOT NULL,
  src_hash   TEXT NOT NULL,
  format_ok  INTEGER NOT NULL DEFAULT 1,   -- 占位符校验通过=1；被翻译/丢失=0（前端灰提示）
  created_at REAL NOT NULL,
  UNIQUE(paper_id, block_id, lang)
);

CREATE TABLE IF NOT EXISTS jobs (
  id         TEXT PRIMARY KEY,
  paper_id   TEXT DEFAULT '',
  kind       TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|error
  progress   REAL DEFAULT 0,
  message    TEXT DEFAULT '',
  payload    TEXT DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL NOT NULL,
  module      TEXT NOT NULL,
  model       TEXT DEFAULT '',
  question    TEXT DEFAULT '',
  prompt      TEXT DEFAULT '',
  answer      TEXT DEFAULT '',
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  latency_ms  INTEGER DEFAULT 0,
  guardrail   TEXT DEFAULT '',               -- 记录 guardrail 事件
  prompt_version TEXT DEFAULT ''             -- 本次调用所用 prompt 版本（审计）
);
"""


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    with _write_lock:
        conn = connect()
        conn.executescript(SCHEMA)
        _migrate_cjk_index(conn)
        _migrate_qa_prompt_version(conn)
        conn.commit()


def _migrate_qa_prompt_version(conn: sqlite3.Connection) -> None:
    """旧库升级：qa_logs 加 prompt_version 列（Prompt 审计，4.5）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(qa_logs)")]
    if cols and "prompt_version" not in cols:
        conn.execute("ALTER TABLE qa_logs ADD COLUMN prompt_version TEXT DEFAULT ''")


def _migrate_cjk_index(conn: sqlite3.Connection) -> None:
    """旧库升级：chunks 加 text_cjk 列并回填，FTS 重建到 text_cjk（无需重解析 PDF）。"""
    from .rag.cjk import cjk_bigram

    chunk_cols = [r[1] for r in conn.execute("PRAGMA table_info(chunks)")]
    added = False
    if chunk_cols and "text_cjk" not in chunk_cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN text_cjk TEXT DEFAULT ''")
        added = True
    fts_cols = [r[1] for r in conn.execute("PRAGMA table_info(chunks_fts)")]
    if fts_cols and "text" in fts_cols and "text_cjk" not in fts_cols:
        conn.execute("DROP TABLE chunks_fts")     # 旧列名 text → 重建为新 schema
        conn.executescript(SCHEMA)
    if added:
        rows = conn.execute("SELECT id, text FROM chunks").fetchall()
        for r in rows:
            conn.execute("UPDATE chunks SET text_cjk=? WHERE id=?", (cjk_bigram(r["text"]), r["id"]))
    # FTS 行数落后于 chunks（重建后）→ 全量回填
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_fts = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    if n_fts < n_chunks:
        rows = conn.execute(
            "SELECT id, paper_id, COALESCE(NULLIF(text_cjk,''), text) FROM chunks ORDER BY paper_id, seq"
        ).fetchall()
        for r in rows:
            conn.execute("INSERT INTO chunks_fts(chunk_id,paper_id,text_cjk) VALUES(?,?,?)", tuple(r))


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """写事务（串行化，SQLite 单写者）。"""
    with _write_lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


# ---------- jobs ----------

def create_job(kind: str, paper_id: str = "", payload: dict | None = None) -> str:
    job_id = new_id("job_")
    with tx() as c:
        c.execute(
            "INSERT INTO jobs(id,paper_id,kind,status,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, paper_id, kind, "queued", json.dumps(payload or {}, ensure_ascii=False), now(), now()),
        )
    return job_id


def update_job(job_id: str, **fields: Any) -> None:
    fields["updated_at"] = now()
    cols = ",".join(f"{k}=?" for k in fields)
    with tx() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id: str) -> dict | None:
    row = query("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not row:
        return None
    d = dict(row[0])
    d["payload"] = json.loads(d["payload"] or "{}")
    return d


# ---------- 审计日志 ----------

def log_qa(module: str, model: str = "", question: str = "", prompt: str = "",
           answer: str = "", prompt_tokens: int = 0, completion_tokens: int = 0,
           latency_ms: int = 0, guardrail: str = "", prompt_version: str = "") -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO qa_logs(ts,module,model,question,prompt,answer,prompt_tokens,"
            "completion_tokens,latency_ms,guardrail,prompt_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now(), module, model, question, prompt[:4000], answer[:8000],
             prompt_tokens, completion_tokens, latency_ms, guardrail, prompt_version),
        )


# ---------- 术语表（4.7 翻译） ----------

def list_terminology() -> list[dict]:
    return [dict(r) for r in query("SELECT id, term, translation, source FROM terminology ORDER BY term")]


def upsert_terminology(term: str, translation: str, source: str = "manual") -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO terminology(term, translation, source, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(term) DO UPDATE SET translation=excluded.translation, "
            "source=excluded.source",
            (term.strip(), translation.strip(), source, now()))


def delete_terminology(term: str) -> None:
    with tx() as c:
        c.execute("DELETE FROM terminology WHERE term=?", (term.strip(),))
