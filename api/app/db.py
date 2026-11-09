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
  status     TEXT NOT NULL DEFAULT 'queued',  -- queued|running|error
  progress   REAL DEFAULT 0,
  message    TEXT DEFAULT '',
  payload    TEXT DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_kind_paper ON jobs(kind, paper_id, created_at);

CREATE TABLE IF NOT EXISTS translation_exports (   -- 模块三 P0-2：导出审计
  id             TEXT PRIMARY KEY,
  paper_id       TEXT NOT NULL,
  fmt            TEXT NOT NULL,             -- pdf | md | html
  target_lang    TEXT NOT NULL DEFAULT 'zh',
  mode           TEXT NOT NULL DEFAULT 'academic',
  layout         TEXT NOT NULL DEFAULT 'overlay',  -- overlay | bilingual | mono
  path           TEXT NOT NULL,
  pages          INTEGER DEFAULT 0,
  replaced       INTEGER DEFAULT 0,
  overflow_count INTEGER DEFAULT 0,
  created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tracking_subscriptions (  -- 模块四 P0-3：arXiv 订阅
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  query_type TEXT NOT NULL,             -- keyword | author | category
  value      TEXT NOT NULL,
  enabled    INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  last_run   REAL NOT NULL DEFAULT 0    -- 水位线：重启续跑/断网补拉的 from 锚点
);

CREATE TABLE IF NOT EXISTS tracked_items (           -- 模块四 P0-3：追踪条目（去重入库）
  id        TEXT PRIMARY KEY,
  sub_id    TEXT NOT NULL,
  arxiv_id  TEXT NOT NULL,
  title     TEXT DEFAULT '',
  published TEXT DEFAULT '',
  authors   TEXT DEFAULT '[]',
  status    TEXT DEFAULT 'new',         -- new | known(库内已有) | dismissed
  added_at  REAL NOT NULL,
  UNIQUE(sub_id, arxiv_id)
);

CREATE TABLE IF NOT EXISTS user_profiles (           -- 模块四 P0-4：多用户画像（冷启动内容推荐）
  user_id   TEXT PRIMARY KEY,
  keywords  TEXT DEFAULT '{}',         -- 关键词 → 计数（反馈 ±加权）
  updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recommendations (         -- 模块四 P0-4：推荐流水（含理由与分量）
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL,
  title      TEXT NOT NULL,
  arxiv_id   TEXT DEFAULT '',
  pdf_url    TEXT DEFAULT '',
  scores     TEXT NOT NULL,            -- JSON：各分量分值（可解释）
  reason     TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS recommend_feedback (      -- 模块四 P0-4：赞/踩（重加权依据）
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  rec_id     TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  vote       INTEGER NOT NULL,         -- 1 赞 | -1 踩
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS survey_reports (           -- 模块五 P0-2：整批综述缓存
  id           TEXT PRIMARY KEY,
  paper_ids_sig TEXT NOT NULL,         -- 排序后 paper_ids 拼接（缓存键）
  stats        TEXT DEFAULT '{}',      -- 图谱统计（节点/边/社区/年份跨度）
  content_md   TEXT DEFAULT '',
  sections     TEXT DEFAULT '[]',      -- [{title, content, claims, anchors}]
  created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS future_checks (           -- 模块五 P0-3：外部进展核查证据
  id             TEXT PRIMARY KEY,
  idea_sig       TEXT NOT NULL,        -- idea 归一签名（去重键）
  idea_text      TEXT NOT NULL,
  source_paper   TEXT DEFAULT '',
  verdict        TEXT NOT NULL,        -- solved | partial | open | open_unverified
  confidence     REAL DEFAULT 0.0,
  evidence       TEXT DEFAULT '[]',    -- [{title, year, citation_count, quote}]
  external_count INTEGER DEFAULT 0,
  checked_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS writing_drafts (           -- 模块六 P0-2：论文框架草稿
  id              TEXT PRIMARY KEY,
  idea            TEXT NOT NULL,
  paper_ids       TEXT DEFAULT '[]',
  outline         TEXT DEFAULT '[]',
  sections        TEXT DEFAULT '[]',
  references_json TEXT DEFAULT '[]',    -- references 为 SQL 关键字，故用 _json 后缀
  guardrails      TEXT DEFAULT '[]',
  created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS csv_datasets (             -- 模块六 P0-3：CSV 数据集
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  columns     TEXT NOT NULL,          -- JSON [{name, dtype}]
  row_count   INTEGER NOT NULL,
  data        TEXT NOT NULL,          -- JSON rows（≤5MB 上限由路由守卫）
  sha256      TEXT DEFAULT '',
  created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS viz_artifacts (            -- 模块六 P0-3：图表与图注产物
  id          TEXT PRIMARY KEY,
  dataset_id  TEXT NOT NULL,
  spec        TEXT NOT NULL,          -- ECharts 兼容 spec（table/line/bar/radar）
  caption     TEXT DEFAULT '',
  created_at  REAL NOT NULL
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

-- 模块一 P0：内容级去重预警（独立于 jobs 表，三态决策落库）
CREATE TABLE IF NOT EXISTS dedup_alerts (
  id               TEXT PRIMARY KEY,
  new_paper_id     TEXT NOT NULL,
  existing_paper_id TEXT NOT NULL,
  score            REAL DEFAULT 0,
  breakdown_json   TEXT DEFAULT '{}',   -- 特征分量 {title,vector,author,minhash,containment,ngram,rerank,llm,year} + 旗标
  reason           TEXT DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'pending',  -- pending|covered|kept|coexisted（LLM 判异族否决 → pending 直接删除，决策审计走 qa_logs）
  created_at       REAL NOT NULL,
  decided_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_dedup_new ON dedup_alerts(new_paper_id, status);
CREATE INDEX IF NOT EXISTS idx_dedup_existing ON dedup_alerts(existing_paper_id, status);

-- 模块一 P0：DiffLens 版本差异结果缓存
CREATE TABLE IF NOT EXISTS version_diffs (
  id           TEXT PRIMARY KEY,
  base_id      TEXT NOT NULL,             -- 旧版本 paper_id
  target_id    TEXT NOT NULL,             -- 新版本 paper_id
  stats_json   TEXT DEFAULT '{}',         -- {sections_changed, paras_changed, numbers_changed}
  payload_json TEXT DEFAULT '{}',         -- 完整对齐 diff 载荷（前端 DiffLens 渲染）
  created_at   REAL NOT NULL,
  UNIQUE(base_id, target_id)
);
"""


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        # FTS5 merge 等操作会写磁盘临时文件；bash 启动环境泄漏 TMPDIR=/tmp 时
        # （Windows 无此路径）报 "unable to open database file"——内存临时存储根治
        conn.execute("PRAGMA temp_store=MEMORY")
        _local.conn = conn
    return conn


def init_db() -> None:
    with _write_lock:
        conn = connect()
        conn.executescript(SCHEMA)
        _migrate_cjk_index(conn)
        _migrate_qa_prompt_version(conn)
        _migrate_papers_module1(conn)
        _migrate_papers_dedup2(conn)
        _migrate_references_rich(conn)
        _migrate_tracked_items_meta(conn)
        conn.commit()


def _migrate_tracked_items_meta(conn: sqlite3.Connection) -> None:
    """Round4：tracked_items 加 abstract/citation_count/venue——推荐五分量中
    citation/keyword 此前对追踪候选恒为中性 0.5/标题词（评审指「假分」）；
    abstract 随抓取带入（OAI dc:description / Atom summary），引用数/venue 由
    推荐 S2 批量懒补回填。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tracked_items)")]
    if not cols:
        return
    for name, decl in {"abstract": "TEXT DEFAULT ''",
                       "citation_count": "INTEGER",
                       "venue": "TEXT DEFAULT ''"}.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE tracked_items ADD COLUMN {name} {decl}")


def _migrate_references_rich(conn: sqlite3.Connection) -> None:
    """Phase 0：references 加结构化列——authors/year/venue/title_norm。
    解析层本就产出 year（旧版落库丢弃）；authors/venue/title_norm 为新增抽取。
    供模块五图谱 title_norm 匹配与模块六写作 References 白名单去重使用。"""
    cols = [r[1] for r in conn.execute('PRAGMA table_info("references")')]
    if not cols:
        return
    add = {
        "authors":    "TEXT DEFAULT ''",   # 作者跨度（title 前段，>180 字符截断 et al.）
        "year":       "INTEGER DEFAULT 0",
        "venue":      "TEXT DEFAULT ''",   # 期刊/会议线索（尾段清洗）
        "title_norm": "TEXT DEFAULT ''",   # 归一标题：白名单/图谱匹配键
    }
    for name, decl in add.items():
        if name not in cols:
            conn.execute(f'ALTER TABLE "references" ADD COLUMN {name} {decl}')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_refs_title_norm ON "references"(title_norm)')


def _migrate_papers_module1(conn: sqlite3.Connection) -> None:
    """旧库升级：papers 加模块一 P0 列（软归档/语义去重/结构化元数据）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(papers)")]
    if not cols:
        return
    add = {
        "family_id":    "TEXT DEFAULT ''",     # 同族键：arxiv 基 id 优先，否则标题归一
        "status":       "TEXT DEFAULT 'active'",   # active | archived（软归档）
        "superseded_by": "TEXT DEFAULT ''",    # 被谁取代（archived 时指向 canonical）
        "abstract":     "TEXT DEFAULT ''",
        "authors_json": "TEXT DEFAULT '[]'",
        "content_sig":  "BLOB",                # 正文 MinHash 指纹（128×f4）
        "paper_vec":    "BLOB",                # title+abstract 论文级向量（float32 LE）
        "meta_source":  "TEXT DEFAULT ''",     # arxiv_api | llm | heuristic（诚实标注）
    }
    for name, decl in add.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {decl}")
    # file_hash 精确去重唯一性（TOCTOU 最后一道防线：并发插入撞唯一索引而失败的一方可安全重查复用）
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_file_hash ON papers(file_hash)")


def _migrate_papers_dedup2(conn: sqlite3.Connection) -> None:
    """旧库升级（9 层方案 L2）：papers 加标识符/元数据列——DOI/年份/ venue。
    metadata.py 三层抽取本就产出这三个字段，落列后进入标识符精确层与 year 特征。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(papers)")]
    if not cols:
        return
    add = {
        "doi":      "TEXT DEFAULT ''",   # 标识符精确层（_norm_doi 归一后比对）
        "pub_year": "INTEGER DEFAULT 0", # year 特征 exp(-|Δ|/3) + LLM 判重提示
        "venue":    "TEXT DEFAULT ''",   # LLM 判重提示（期刊/会议线索）
    }
    for name, decl in add.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {decl}")


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


def job_cancel_requested(job_id: str) -> bool:
    """协作式取消探测：解析阶段边界轮询（SQLite 读，开销可忽略）。"""
    rows = query("SELECT status FROM jobs WHERE id=?", (job_id,))
    return bool(rows) and rows[0]["status"] == "cancel_requested"


# ---------- dedup_alerts（模块一 P0：三态去重决策） ----------

def create_dedup_alert(new_paper_id: str, existing_paper_id: str, score: float,
                       breakdown: dict, reason: str) -> str | None:
    """幂等插入：同 (new, existing) 对已有 pending alert 时复用其 id。"""
    alert_id = new_id("da_")
    with tx() as c:
        row = c.execute(
            "SELECT id FROM dedup_alerts WHERE new_paper_id=? AND existing_paper_id=? "
            "AND status='pending'", (new_paper_id, existing_paper_id)).fetchone()
        if row:
            return row["id"]
        c.execute(
            "INSERT INTO dedup_alerts(id,new_paper_id,existing_paper_id,score,breakdown_json,"
            "reason,status,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
            (alert_id, new_paper_id, existing_paper_id, score,
             json.dumps(breakdown, ensure_ascii=False), reason, now()))
    return alert_id


def pending_alerts_for(paper_id: str) -> list[dict]:
    """某篇论文相关的全部 pending 预警（双向：它是新上传方或既有方）。"""
    rows = query(
        "SELECT * FROM dedup_alerts WHERE status='pending' "
        "AND (new_paper_id=? OR existing_paper_id=?) ORDER BY created_at DESC",
        (paper_id, paper_id))
    return [dict(r) | {"breakdown": json.loads(r["breakdown_json"] or "{}")} for r in rows]


def delete_pending_alert_pair(id_a: str, id_b: str) -> int:
    """删除双向 pending 预警（L7 LLM 复核重写前置步骤；已决策的终态不受影响）。"""
    with tx() as c:
        cur = c.execute(
            "DELETE FROM dedup_alerts WHERE status='pending' AND "
            "((new_paper_id=? AND existing_paper_id=?) OR "
            "(new_paper_id=? AND existing_paper_id=?))",
            (id_a, id_b, id_b, id_a))
        return cur.rowcount


def get_dedup_alert(alert_id: str) -> dict | None:
    rows = query("SELECT * FROM dedup_alerts WHERE id=?", (alert_id,))
    if not rows:
        return None
    d = dict(rows[0])
    d["breakdown"] = json.loads(d["breakdown_json"] or "{}")
    return d


# ---------- version_diffs（模块一 P0：DiffLens） ----------

def save_version_diff(base_id: str, target_id: str, stats: dict, payload: dict) -> str:
    with tx() as c:
        row = c.execute("SELECT id FROM version_diffs WHERE base_id=? AND target_id=?",
                        (base_id, target_id)).fetchone()
        if row:
            c.execute("UPDATE version_diffs SET stats_json=?, payload_json=?, created_at=? "
                      "WHERE id=?",
                      (json.dumps(stats, ensure_ascii=False),
                       json.dumps(payload, ensure_ascii=False), now(), row["id"]))
            return row["id"]
        did = new_id("vd_")
        c.execute(
            "INSERT INTO version_diffs(id,base_id,target_id,stats_json,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (did, base_id, target_id, json.dumps(stats, ensure_ascii=False),
             json.dumps(payload, ensure_ascii=False), now()))
        return did


def get_version_diff(base_id: str, target_id: str) -> dict | None:
    rows = query("SELECT * FROM version_diffs WHERE base_id=? AND target_id=?",
                 (base_id, target_id))
    if not rows:
        return None
    d = dict(rows[0])
    d["stats"] = json.loads(d["stats_json"] or "{}")
    d["payload"] = json.loads(d["payload_json"] or "{}")
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
