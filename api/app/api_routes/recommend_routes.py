"""推荐端点（模块四 P0-4）：推荐流 + 赞/踩反馈。多用户经 X-User-Id 头隔离。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from ..agents import recommend

router = APIRouter(prefix="/api")

_MAX_USER_ID = 64


def _uid(x_user_id: str | None) -> str:
    """匿名多用户：前端 localStorage uuid；缺省 anonymous。长度/字符护栏防注入。"""
    u = (x_user_id or "anonymous").strip()
    if len(u) > _MAX_USER_ID or not u.replace("-", "").replace("_", "").isalnum():
        u = "anonymous"
    return u


@router.get("/recommendations")
def get_recommendations(x_user_id: str | None = Header(None), limit: int = 10,
                        refresh: bool = False):
    """推荐流。refresh=true 重新打分生成（候选=追踪新条目）；默认读上次结果。"""
    user_id = _uid(x_user_id)
    if refresh:
        out = recommend.generate(user_id, limit)
        if "note" in out:
            return out | {"recommendations": recommend.list_for(user_id)}
    return {"user_id": user_id, "recommendations": recommend.list_for(user_id)}


@router.post("/recommendations/{rec_id}/feedback")
def feedback(rec_id: str, body: dict, x_user_id: str | None = Header(None)):
    if body.get("vote") not in (1, -1):
        raise HTTPException(400, "vote 只支持 1(赞) | -1(踩)")
    try:
        return recommend.record_feedback(rec_id, _uid(x_user_id), int(body["vote"]))
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
