"""记忆与状态端点（模块七 R4，清单 §三#16）：研究时间线 / 假设 FSM 状态板 / 开场研究简报。

- GET /api/memory/timeline：记忆事件流（时间区间 / kind / 会话过滤；时间输出 ISO 8601），
  附返回事件集内部的因果边（/memory 页 G6 图数据源）。
- GET /api/memory/hypotheses：假设事件 + FSM 状态 + 转移历史 + 合法下一态。
- POST /api/memory/brief：按需构建研究简报（假设 vs 新证据；unverified 不冒充）。
P2 另有 PATCH append-only 修正入口（清单 §2.6）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..memory import briefing, store
from ..memory.timeparse import LOCAL_TZ

router = APIRouter(prefix="/api/memory")

_TZ8 = timezone(timedelta(hours=8))


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=_TZ8).isoformat()


def _parse_bound(value: str, name: str) -> float:
    """ISO 8601（含 Z / 偏移 / 纯日期）→ UTC epoch；naive 按 +08:00 本地时区解释。"""
    try:
        dt = datetime.fromisoformat(value)   # 3.11+ 原生支持 Z 后缀与偏移
    except ValueError as e:
        raise HTTPException(400, f"{name} 不是合法 ISO 8601 时间：{value}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.timestamp()


def _dump_public(ev: dict) -> dict:
    """事件 → API 形态：时间出 ISO 8601（DB 内为 UTC epoch，清单 §2.4）。"""
    return {k: v for k, v in ev.items() if k != "vec"} | {
        "ts_iso": _iso(ev["ts"]),
        "t_valid_from_iso": _iso(ev["t_valid_from"]),
        "t_valid_to_iso": _iso(ev["t_valid_to"]),
        "last_access_iso": _iso(ev["last_access"]) if ev["last_access"] else None,
    }


@router.get("/timeline")
def timeline(start: str = "", end: str = "", kind: str = "", status: str = "",
             conversation_id: str = "", expr: str = "",
             limit: int = Query(default=200, le=500)):
    """expr（模块七时间表达式筛选框）：自然语言（"上季度/最近两周/last month"）→
    timeparse 解析为区间，与显式 start/end 互斥（expr 优先）。"""
    t_start = _parse_bound(start, "start") if start else None
    t_end = _parse_bound(end, "end") if end else None
    expr_meta = None
    if expr:
        from ..memory import timeparse
        hit = timeparse.parse_expr(expr)
        if not hit:
            raise HTTPException(400, f"无法解析时间表达式：{expr}")
        t_start, t_end = hit.t_start, hit.t_end
        expr_meta = {"expr": hit.expr, "start_iso": _iso(hit.t_start), "end_iso": _iso(hit.t_end)}
    events = store.list_events(kind=kind, status=status, conversation_id=conversation_id,
                               t_start=t_start, t_end=t_end, limit=limit)
    ids = {e["id"] for e in events}
    edges = [e for e in store.list_edges(status="active")
             if e["src"] in ids and e["dst"] in ids]
    return {"events": [_dump_public(e) for e in events],
            "edges": edges,
            "meta": {"n_events": len(events), "n_edges": len(edges),
                     "start_iso": _iso(t_start), "end_iso": _iso(t_end),
                     "time_range": expr_meta}}


@router.get("/hypotheses")
def hypotheses(status: str = ""):
    """假设 FSM 状态板数据：状态 + 转移历史 + 合法下一态（store.HYPOTHESIS_TRANSITIONS）。"""
    events = store.list_events(kind="hypothesis", status=status, limit=200)
    out = []
    for ev in events:
        pub = _dump_public(ev)
        pub["transitions"] = (ev.get("meta") or {}).get("transitions") or []
        pub["legal_next"] = sorted(store.HYPOTHESIS_TRANSITIONS.get(ev["status"], set()))
        out.append(pub)
    return {"hypotheses": out, "meta": {"n": len(out)}}


class BriefBody(BaseModel):
    conversation_id: str = ""    # 空 = 用户级全量假设；给定则限定该会话
    use_judge: bool = True       # False 强制走规则路（离线评测/演示确定性）


@router.post("/brief")
def brief(body: BriefBody):
    # use_judge 直通 build_brief：False=强制规则/图谱边判定（确定性口径，chat 开场用）；
    # True=LLM 可用时 judge。engine 字段由 build_brief 按实际判定路径如实写入——
    # 不得事后改标签（R5 修复：此前只改 engine 字符串不传参，判定源与标注不一致）。
    return briefing.build_brief(conversation_id=body.conversation_id,
                                use_judge=body.use_judge)


@router.get("/events/{event_id}")
def get_event(event_id: str):
    ev = store.get_event(event_id)
    if not ev:
        raise HTTPException(404, f"记忆事件不存在：{event_id}")
    return _dump_public(ev)


class PatchBody(BaseModel):
    content: str = ""    # 内容修正 → append-only：新事件（source=manual）+ 旧事件 superseded
    status: str = ""     # 状态修正 → FSM 合法转移（非法转移 400，转移历史入 transitions[]）
    note: str = ""       # 修正原因（审计）


@router.patch("/events/{event_id}")
def patch_event(event_id: str, body: PatchBody):
    """手动修正（清单 §2.6，MemGPT self-editing 人工版）：
    - content 修正**不原地改**：新事件（source=manual，meta.corrected_from 指向旧 id）
      + 旧事件 superseded——append-only 审计链；修正后的事实重新进入活跃集
      （终态旧事件被修正 → 新事件回到 active，待简报/worker 重新判定，如实记录）；
    - status 修正走 FSM 合法转移表（仅 hypothesis），非法转移 400。"""
    ev = store.get_event(event_id)
    if not ev:
        raise HTTPException(404, f"记忆事件不存在：{event_id}")
    if not body.content and not body.status:
        raise HTTPException(400, "至少提供 content 或 status 之一")
    if body.content and body.content != ev["content"]:
        new_status = ev["status"] if ev["status"] in ("proposed", "active") else "active"
        new_ev = store.add_event(
            ev["kind"], body.content, user_id=ev["user_id"],
            conversation_id=ev["conversation_id"], turn=ev["turn"],
            thread_id=ev["thread_id"], subject=ev["subject"],
            entities=ev["entities"], t_valid_from=ev["t_valid_from"],
            t_valid_to=ev["t_valid_to"], importance=ev["importance"],
            status=new_status, source="manual",
            meta={"corrected_from": event_id, "correction_note": body.note})
        store.supersede_event(event_id, by_id=new_ev["id"],
                              why=body.note or "手动修正内容", source="manual")
        return {"mode": "corrected", "new_event": _dump_public(store.get_event(new_ev["id"])),
                "superseded": event_id}
    if body.status:
        if ev["kind"] != "hypothesis":
            raise HTTPException(400, "仅 hypothesis 事件支持状态修正（FSM 只作用于假设）")
        try:
            updated = store.transition(event_id, body.status,
                                       why=body.note or "手动状态修正",
                                       verdict_source="manual")
        except store.FSMError as e:
            raise HTTPException(400, str(e)) from e
        return {"mode": "transition", "event": _dump_public(updated)}
    raise HTTPException(400, "content 与原内容相同，无需修正")
