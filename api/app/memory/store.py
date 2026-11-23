"""记忆与状态层存储（模块七 R1）：三表 CRUD + 假设生命周期 FSM + 时间因果边。

设计约定（清单 §2.2/§2.3/§2.5/§2.6）：
- 记忆事件 append-only：content 不原地改（P2 手动修正走「新事件 + 旧事件 superseded」）；
- FSM 只作用于 kind=hypothesis：合法转移表强制，非法转移抛 FSMError
  （API 层转 400，后台链路 log.warning，不落 qa_logs——那是问答链路审计表）；
  转移历史 append-only 记入 meta_json.transitions[]；
- 边 UNIQUE(src,dst,relation) + UPSERT：同三元组再次确认 confirm_count+=1、保留最早
  t_valid_from；supports↔refutes 互斥翻转不 UPSERT——旧边标 superseded + 新边插入；
  被 superseded 的三元组再次出现 → 复活（新有效周期，t_valid_from=now）。
- 时间戳一律 UTC epoch（timeparse 负责自然语言 → 区间）。
"""
from __future__ import annotations

import json
import logging

from ..db import new_id, now, query, tx

log = logging.getLogger("meridian.memory")

EVENT_KINDS = {"hypothesis", "experiment_result", "finding", "decision", "preference", "topic"}
EVENT_SOURCES = {"llm", "rule", "manual", "agent"}
RELATIONS = {"refutes", "supersedes", "supports", "elaborates", "same_topic", "cause"}
# supports↔refutes 语义互斥（同对实体同一时刻只能「支持」或「反驳」其一为真）
_CONFLICTING = {"supports": "refutes", "refutes": "supports"}

# 假设生命周期 FSM（清单 §2.2）：proposed（初始）→ active（出现首条同主题证据）
# → supported/refuted（judge/worker 复核判定）→ superseded（同主题新假设/手动修正）。
# superseded 为终态；supported/refuted 仍可被取代（保留信念演化史）。
HYPOTHESIS_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"active", "superseded"},
    "active": {"supported", "refuted", "superseded"},
    "supported": {"superseded"},
    "refuted": {"superseded"},
    "superseded": set(),
}


class FSMError(ValueError):
    """非法状态转移（guardrail 证据，不是 bug）：API 层转 400。"""


# ---------- 序列化 ----------

def _dump_event(row) -> dict:
    d = dict(row)
    d["entities"] = json.loads(d.pop("entities_json") or "[]")
    d["meta"] = json.loads(d.pop("meta_json") or "{}")
    return d


def _dump_edge(row) -> dict:
    d = dict(row)
    d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
    return d


# ---------- 记忆事件 ----------

def add_event(kind: str, content: str, *, conversation_id: str = "", turn: int = 0,
              thread_id: str = "", user_id: str = "local", subject: str = "",
              entities: list | None = None, t_valid_from: float | None = None,
              t_valid_to: float | None = None, importance: int = 5,
              confidence: float = 0.0, verdict_source: str = "", source: str = "rule",
              status: str = "", ts: float | None = None, vec: bytes | None = None,
              meta: dict | None = None) -> dict:
    """新增记忆事件（append-only）。hypothesis 初始态 proposed（FSM 起点），
    其余 kind 初始态 active。strength 初值 = importance/10（清单 §2.3）。"""
    if kind not in EVENT_KINDS:
        raise ValueError(f"未知记忆类型 kind={kind}，合法值：{sorted(EVENT_KINDS)}")
    if source not in EVENT_SOURCES:
        raise ValueError(f"未知来源 source={source}，合法值：{sorted(EVENT_SOURCES)}")
    if not 1 <= int(importance) <= 10:
        raise ValueError(f"importance 须为 1-10，得到 {importance}")
    if not status:
        status = "proposed" if kind == "hypothesis" else "active"
    eid = new_id("me_")
    with tx() as c:
        c.execute(
            "INSERT INTO memory_events(id,user_id,conversation_id,turn,thread_id,ts,kind,"
            "content,subject,entities_json,t_valid_from,t_valid_to,status,confidence,"
            "verdict_source,importance,strength,vec,source,meta_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, user_id, conversation_id, turn, thread_id,
             ts if ts is not None else now(), kind, content, subject,
             json.dumps(entities or [], ensure_ascii=False),
             t_valid_from, t_valid_to, status, confidence, verdict_source,
             int(importance), int(importance) / 10.0, vec, source,
             json.dumps(meta or {}, ensure_ascii=False)))
    return get_event(eid)  # type: ignore[return-value]


def get_event(event_id: str) -> dict | None:
    rows = query("SELECT * FROM memory_events WHERE id=?", (event_id,))
    return _dump_event(rows[0]) if rows else None


def list_events(*, kind: str = "", status: str = "", conversation_id: str = "",
                t_start: float | None = None, t_end: float | None = None,
                limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM memory_events WHERE 1=1"
    params: list = []
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    if status:
        sql += " AND status=?"
        params.append(status)
    if conversation_id:
        sql += " AND conversation_id=?"
        params.append(conversation_id)
    if t_start is not None:
        sql += " AND ts>=?"
        params.append(t_start)
    if t_end is not None:
        sql += " AND ts<=?"
        params.append(t_end)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(int(limit))
    return [_dump_event(r) for r in query(sql, tuple(params))]


def transition(event_id: str, to_status: str, *, why: str = "",
               verdict_source: str = "", at: float | None = None) -> dict:
    """FSM 转移（仅 hypothesis）。合法转移表见 HYPOTHESIS_TRANSITIONS；
    转移历史 append-only 追加 meta_json.transitions[]（审计链）。"""
    ev = get_event(event_id)
    if not ev:
        raise KeyError(f"记忆事件不存在：{event_id}")
    if ev["kind"] != "hypothesis":
        raise FSMError(f"非假设事件不参与状态机：kind={ev['kind']}")
    frm = ev["status"]
    if to_status not in HYPOTHESIS_TRANSITIONS.get(frm, set()):
        raise FSMError(f"非法状态转移 {frm}→{to_status}（guardrail：合法转移表拒绝）")
    meta = ev["meta"]
    meta.setdefault("transitions", []).append(
        {"from": frm, "to": to_status, "at": at if at is not None else now(),
         "why": why, "verdict_source": verdict_source})
    with tx() as c:
        c.execute("UPDATE memory_events SET status=?, verdict_source=?, meta_json=? WHERE id=?",
                  (to_status, verdict_source or ev["verdict_source"],
                   json.dumps(meta, ensure_ascii=False), event_id))
    out = get_event(event_id)
    assert out is not None
    return out


def supersede_event(event_id: str, *, by_id: str = "", why: str = "",
                    source: str = "manual") -> dict:
    """便捷 supersede（幂等：已 superseded 原样返回）。手动修正 / 同主题新假设均走此入口。"""
    ev = get_event(event_id)
    if not ev:
        raise KeyError(f"记忆事件不存在：{event_id}")
    if ev["status"] == "superseded":
        return ev
    out = transition(event_id, "superseded",
                     why=why or ("被新事件取代" if by_id else source),
                     verdict_source="manual" if source == "manual" else "")
    if by_id:
        meta = out["meta"]
        meta["superseded_by"] = by_id
        with tx() as c:
            c.execute("UPDATE memory_events SET meta_json=? WHERE id=?",
                      (json.dumps(meta, ensure_ascii=False), event_id))
    out = get_event(event_id)
    assert out is not None
    return out


def touch_access(event_id: str, *, at: float | None = None) -> None:
    """检索命中后更新 last_access/access_count（三信号 recency 的依据）。"""
    t = at if at is not None else now()
    with tx() as c:
        c.execute("UPDATE memory_events SET access_count=access_count+1, last_access=? WHERE id=?",
                  (t, event_id))


# ---------- 实体消解（规则版；P1 升级 LLM 别名判定） ----------

def _norm_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def resolve_entity(name: str, *, user_id: str = "local", etype: str = "") -> dict:
    """规则归一（大小写/空白）后按 canonical/别名精确匹配；未命中则建新实体。"""
    norm = _norm_name(name)
    rows = query("SELECT * FROM entities WHERE user_id=?", (user_id,))
    for r in rows:
        if _norm_name(r["canonical_name"]) == norm or norm in json.loads(r["aliases_json"] or "[]"):
            return dict(r)
    eid = new_id("ent_")
    with tx() as c:
        c.execute("INSERT INTO entities(id,user_id,canonical_name,aliases_json,type,created_at) "
                  "VALUES(?,?,?,?,?,?)",
                  (eid, user_id, name.strip(),
                   json.dumps(sorted({norm}), ensure_ascii=False), etype, now()))
    return dict(query("SELECT * FROM entities WHERE id=?", (eid,))[0])


# ---------- 时间因果边 ----------

def upsert_edge(src: str, dst: str, relation: str, *, kind: str = "rule",
                evidence: dict | None = None, verdict_source: str = "",
                ts: float | None = None, t_valid_from: float | None = None) -> dict:
    """插入/确认一条因果边（清单 §2.5）：
    - 同三元组已有 active 边 → confirm_count+=1、ts 更新、evidence 追加、保留最早 t_valid_from；
    - 同三元组已被 superseded → 复活（status=active，新有效周期 t_valid_from=now）；
    - supports↔refutes 互斥翻转 → 先把旧边标 superseded（保留演化史）再走上述分支。"""
    if relation not in RELATIONS:
        raise ValueError(f"未知关系 relation={relation}，合法值：{sorted(RELATIONS)}")
    t = ts if ts is not None else now()
    conflict = _CONFLICTING.get(relation)
    if conflict:
        rows = query("SELECT evidence_json FROM memory_edges WHERE src=? AND dst=? AND relation=? "
                     "AND status='active'", (src, dst, conflict))
        if rows:
            ev = json.loads(rows[0]["evidence_json"] or "{}")
            ev["superseded_by_relation"] = relation
            with tx() as c:
                c.execute("UPDATE memory_edges SET status='superseded', evidence_json=? "
                          "WHERE src=? AND dst=? AND relation=? AND status='active'",
                          (json.dumps(ev, ensure_ascii=False), src, dst, conflict))
    rows = query("SELECT * FROM memory_edges WHERE src=? AND dst=? AND relation=?",
                 (src, dst, relation))
    if rows:
        row = rows[0]
        ev = json.loads(row["evidence_json"] or "{}")
        if evidence:
            ev.setdefault("evidence", []).append(evidence)
        revived = row["status"] != "active"
        vf = t if revived else (min(row["t_valid_from"], t) if row["t_valid_from"] is not None else t)
        with tx() as c:
            c.execute("UPDATE memory_edges SET confirm_count=confirm_count+1, evidence_json=?, "
                      "verdict_source=?, ts=?, t_valid_from=?, status='active', kind=? WHERE id=?",
                      (json.dumps(ev, ensure_ascii=False), verdict_source or row["verdict_source"],
                       t, vf, kind, row["id"]))
        return _dump_edge(query("SELECT * FROM memory_edges WHERE id=?", (row["id"],))[0])
    eid = new_id("edge_")
    with tx() as c:
        c.execute("INSERT INTO memory_edges(id,src,dst,relation,kind,confirm_count,evidence_json,"
                  "verdict_source,ts,t_valid_from,status) VALUES(?,?,?,?,?,?,?,?,?,?,'active')",
                  (eid, src, dst, relation, kind, 1,
                   json.dumps(({"evidence": [evidence]} if evidence else {}),
                              ensure_ascii=False),
                   verdict_source, t, t_valid_from if t_valid_from is not None else t))
    return _dump_edge(query("SELECT * FROM memory_edges WHERE id=?", (eid,))[0])


def list_edges(*, src: str = "", dst: str = "", relation: str = "",
               status: str = "active") -> list[dict]:
    sql = "SELECT * FROM memory_edges WHERE 1=1"
    params: list = []
    if src:
        sql += " AND src=?"
        params.append(src)
    if dst:
        sql += " AND dst=?"
        params.append(dst)
    if relation:
        sql += " AND relation=?"
        params.append(relation)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY ts DESC"
    return [_dump_edge(r) for r in query(sql, tuple(params))]
