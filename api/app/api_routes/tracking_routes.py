"""arXiv 追踪端点（模块四 P0-3）：订阅 CRUD / 手动刷新 / 条目查询与忽略。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..agents import tracker

router = APIRouter(prefix="/api/tracking")

_TYPES = ("keyword", "author", "category")


class SubBody(BaseModel):
    name: str
    query_type: str = "keyword"   # keyword | author | category(arXiv 分类如 cs.CL)
    value: str


@router.get("/subscriptions")
def list_subs():
    rows = db.query("SELECT * FROM tracking_subscriptions ORDER BY created_at DESC")
    counts = {r["sub_id"]: r["n"] for r in db.query(
        "SELECT sub_id, COUNT(*) n FROM tracked_items WHERE status='new' GROUP BY sub_id")}
    return {"subscriptions": [dict(r) | {"new_count": counts.get(r["id"], 0)} for r in rows]}


@router.post("/subscriptions")
def add_sub(body: SubBody):
    if body.query_type not in _TYPES:
        raise HTTPException(400, f"query_type 只支持 {' | '.join(_TYPES)}")
    if not body.value.strip() or not body.name.strip():
        raise HTTPException(400, "name/value 不能为空")
    import time
    sub_id = db.new_id("ts_")
    with db.tx() as c:
        c.execute("INSERT INTO tracking_subscriptions(id,name,query_type,value,created_at)"
                  " VALUES(?,?,?,?,?)",
                  (sub_id, body.name.strip(), body.query_type, body.value.strip(), time.time()))
    return {"ok": True, "subscription": {"id": sub_id, "name": body.name.strip(),
                                         "query_type": body.query_type,
                                         "value": body.value.strip()}}


@router.delete("/subscriptions/{sub_id}")
def del_sub(sub_id: str):
    with db.tx() as c:
        c.execute("DELETE FROM tracked_items WHERE sub_id=?", (sub_id,))
        cur = c.execute("DELETE FROM tracking_subscriptions WHERE id=?", (sub_id,))
    if not cur.rowcount:
        raise HTTPException(404, "订阅不存在")
    return {"ok": True}


@router.post("/refresh")
def refresh(sub_id: str | None = None):
    """手动刷新（全部或单个）。同步执行（单订阅 ≤ tracking_max_items 条，秒级）；
    每订阅返回通道（sickle|atom）、新增数与去重数——断网时两通道都失败则如实报错。"""
    rows = db.query("SELECT id FROM tracking_subscriptions WHERE enabled=1"
                    + (" AND id=?" if sub_id else ""),
                    ((sub_id,) if sub_id else ()))
    if sub_id and not rows:
        raise HTTPException(404, "订阅不存在")
    try:
        return {"results": tracker.refresh_all(sub_id)}
    except Exception as e:
        raise HTTPException(502, f"刷新失败：{type(e).__name__}: {e}") from e


@router.get("/items")
def items(sub_id: str | None = None, status: str | None = None, limit: int = 100):
    return {"items": tracker.list_items(sub_id, status, limit)}


@router.post("/items/{item_id}/dismiss")
def dismiss(item_id: str):
    if not tracker.dismiss_item(item_id):
        raise HTTPException(404, "条目不存在")
    return {"ok": True}
