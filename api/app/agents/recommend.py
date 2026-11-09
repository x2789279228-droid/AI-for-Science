"""个性化推荐（模块四 P0-4）：本地库画像 → 候选打分 → 可解释理由 → 反馈重加权。

打分（各分量 0~1 加权，分量全部落库可解释）：
  local_sim 0.35（候选 title/摘要 token 与库内画像关键词的 Jaccard——确定性、离线可用；
             fastembed 向量在本环境限流不稳，不作为唯一依据）
  recency   0.20（发表年份指数衰减）
  citation  0.20（log 归一引用数——"引用含金量"信号；缺数据按 0.5 中性）
  github    0.15（_github_repro 复现分——赛题"优先推荐带 GitHub 代码"；查询失败该分量
             记 None 不计权，理由不冒充）
  keyword   0.10（命中画像关键词个数归一）
多用户：user_id 为隔离键（前端 localStorage uuid 匿名态）；冷启动纯内容画像（库内论文
标题词频），反馈 👍 将候选关键词计数 +2、👎 −1（简单可解释的重加权，不做协同过滤）。
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import date

from .. import db
from ..log import logger

_STOP = {"the", "a", "an", "of", "for", "and", "or", "in", "on", "to", "with", "by",
         "from", "at", "is", "are", "as", "that", "this", "we", "our", "its", "via",
         "based", "using", "toward", "towards", "how", "why"}

_W = {"local": 0.35, "recency": 0.20, "citation": 0.20, "github": 0.15, "keyword": 0.10}


def _tokens(text: str) -> set[str]:
    """小写词元 + 轻量词干归一（去复数 s/es）——"Transformers"↔"transformer" 命中。"""
    out = set()
    for w in re.findall(r"[a-z]{3,}", (text or "").lower()):
        if w in _STOP:
            continue
        if len(w) > 4 and w.endswith("es"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def profile_keywords(user_id: str, limit: int = 60) -> dict[str, float]:
    """用户画像关键词：库内 active 论文标题词频 ∪ 已有画像（反馈累计）。"""
    counts: dict[str, float] = {}
    for r in db.query("SELECT title FROM papers WHERE status='active' AND title!=''"):
        for t in _tokens(r["title"]):
            counts[t] = counts.get(t, 0) + 1
    row = db.query("SELECT keywords FROM user_profiles WHERE user_id=?", (user_id,))
    if row and row[0]["keywords"]:
        try:
            for k, v in json.loads(row[0]["keywords"]).items():
                counts[k] = counts.get(k, 0) + float(v)
        except (ValueError, TypeError):
            pass
    top = dict(sorted(counts.items(), key=lambda x: -x[1])[:limit])
    return top


def _score_candidate(cand: dict, kw: dict[str, float], *,
                     with_github: bool = True) -> dict:
    """单候选打分：返回 {scores(各分量), total, reason}。github 查询失败 → 分量 None。"""
    cand_tokens = _tokens((cand.get("title") or "") + " " + (cand.get("abstract") or "")[:300])
    kw_keys = set(kw)
    inter = cand_tokens & kw_keys
    local = (len(inter) / min(len(cand_tokens), 20)) if cand_tokens else 0.0
    local = min(1.0, local * 2)                      # Jaccard 变体归一
    year = cand.get("year") or int((cand.get("published") or "0")[:4] or 0) or 0
    recency = max(0.0, 1.0 - (date.today().year - year) / 5.0) if year else 0.5
    cc = cand.get("citation_count")
    citation = (min(1.0, math.log1p(cc) / math.log(1000)) if isinstance(cc, (int, float))
                else 0.5)
    keyword = min(1.0, len([k for k in inter if kw.get(k, 0) >= 2]) / 5.0)

    github = None
    repo = None
    if with_github and cand.get("title"):
        try:
            from .tools import _github_repro
            r = _github_repro(cand["title"])
            repos = (r.get("repos") or []) if isinstance(r, dict) else []
            if r.get("ok") and repos:
                repo = repos[0]
                github = float(repo.get("repro_score") or 0.0)
            elif r.get("ok"):
                github = 0.0
        except Exception as e:  # noqa: BLE001 —— GitHub 限流/断网：分量缺失不冒充
            logger.warning(f"recommend github_repro 失败: {type(e).__name__}: {e}",
                           module="recommend")

    scores = {"local": round(local, 3), "recency": round(recency, 3),
              "citation": round(citation, 3), "keyword": round(keyword, 3),
              "github": round(github, 3) if github is not None else None}
    total = (_W["local"] * local + _W["recency"] * recency
             + _W["citation"] * citation + _W["keyword"] * keyword)
    if github is not None:
        total += _W["github"] * github
    reason_parts = []
    if inter:
        reason_parts.append("与你库内方向命中：" + "、".join(sorted(inter)[:4]))
    if year:
        reason_parts.append(f"{year} 年新作")
    if isinstance(cc, (int, float)) and cc >= 50:
        reason_parts.append(f"被引 {cc}")
    if repo:
        reason_parts.append(f"GitHub {repo.get('full_name', '')}（★{repo.get('stars', '?')}，"
                            f"复现分 {repo.get('repro_score', '?')}）")
    if github is None:
        reason_parts.append("GitHub 未查证（网络不可用）")
    return {"scores": scores, "total": round(total, 3),
            "reason": " · ".join(reason_parts) or "来自你的追踪订阅"}


def generate(user_id: str, limit: int = 10, *, with_github: bool = True) -> dict:
    """生成推荐：候选=追踪新条目（未订阅→空，如实返回）。落 recommendations 表。"""
    kw = profile_keywords(user_id)
    cands = db.query(
        "SELECT arxiv_id, title, published FROM tracked_items WHERE status='new'"
        " ORDER BY added_at DESC LIMIT 60")
    if not cands:
        return {"user_id": user_id, "recommendations": [],
                "note": "暂无候选：先在「追踪订阅」添加订阅并刷新（推荐不凭空编造候选）"}

    rows = []
    for c in cands:
        s = _score_candidate({"title": c["title"], "published": c["published"],
                              "year": 0, "citation_count": None, "abstract": ""},
                             kw, with_github=with_github)
        rows.append({"title": c["title"], "arxiv_id": c["arxiv_id"], **s})
    rows.sort(key=lambda x: -x["total"])

    now = time.time()
    keep = rows[: max(1, min(limit, 20))]
    with db.tx() as cx:
        cx.execute("DELETE FROM recommendations WHERE user_id=?", (user_id,))
        for r in keep:
            cx.execute("INSERT INTO recommendations(id,user_id,title,arxiv_id,pdf_url,scores,"
                       "reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                       (db.new_id("r_"), user_id, r["title"][:300], r.get("arxiv_id", ""),
                        "", json.dumps(r["scores"], ensure_ascii=False), r["reason"], now))
    return {"user_id": user_id,
            "recommendations": [{**r, "id": None} for r in keep],
            "profile_keywords": dict(list(kw.items())[:10])}


def list_for(user_id: str) -> list[dict]:
    rows = db.query("SELECT * FROM recommendations WHERE user_id=? ORDER BY created_at DESC",
                    (user_id,))
    out = []
    for r in rows:
        votes = db.query("SELECT COALESCE(SUM(vote),0) v FROM recommend_feedback WHERE rec_id=?",
                         (r["id"],))[0]["v"]
        out.append({"id": r["id"], "title": r["title"], "arxiv_id": r["arxiv_id"],
                    "scores": json.loads(r["scores"] or "{}"), "reason": r["reason"],
                    "my_vote": votes})
    return out


def record_feedback(rec_id: str, user_id: str, vote: int) -> dict:
    """赞/踩：落 recommend_feedback；候选关键词写入画像计数（👍+2 / 👎-1，下限 0）。"""
    row = db.query("SELECT * FROM recommendations WHERE id=?", (rec_id,))
    if not row:
        raise LookupError("recommendation_not_found")
    with db.tx() as c:
        c.execute("INSERT INTO recommend_feedback(rec_id,user_id,vote,created_at)"
                  " VALUES(?,?,?,?)", (rec_id, user_id, vote, time.time()))
    # 画像重加权：候选标题 token 计数调整
    delta = 2.0 if vote > 0 else -1.0
    kw = profile_keywords(user_id)
    for t in _tokens(row[0]["title"]):
        kw[t] = max(0.0, kw.get(t, 0) + delta)
    with db.tx() as c:
        c.execute("INSERT INTO user_profiles(user_id,keywords,updated_at) VALUES(?,?,?)"
                  " ON CONFLICT(user_id) DO UPDATE SET keywords=excluded.keywords,"
                  " updated_at=excluded.updated_at",
                  (user_id, json.dumps(kw, ensure_ascii=False), time.time()))
    return {"ok": True, "rec_id": rec_id, "vote": vote}
