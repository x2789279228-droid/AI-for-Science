"""时间感知记忆检索（模块七 R3，清单 §三#6 / §2.3 / §2.10）。

三信号（Generative Agents）：recency=0.995^距上次访问小时数 · importance(1-10) · relevance；
各信号 min-max 归一化后**等权相加**（原论文即加权求和，非乘积）× FadeMem 强度乘子
（§2.3：只影响排序不物理删除）。
时间窗：timeparse 自然语言 → UTC 区间 ∩ 事件有效窗（t_valid 缺省回落 ts / +inf）。
实体过滤：query 命中已知实体（canonical/别名）→ 限定该实体的事件。
多跳：种子集沿 active 边 BFS 深度≤2，邻居得分 ×0.5^hop（清单 §三#6）。
降级（§2.10）：embedding off/无向量 → relevance 信号回落为 cjk bigram 包含度（词面通道，
等价于关键词兜底而非 FTS5 新表——memory_events 无 FTS 索引，如实记 meta.embed）。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from ..config import settings
from ..rag.cjk import cjk_bigram
from ..rag.indexing import _pack, _query_vector, _unpack
from . import memory_qa, store, timeparse

INF = float("inf")

# Generative Agents recency 指数衰减基数（每小时 ×0.995）
RECENCY_DECAY = 0.995


def _bigrams(text: str) -> set[str]:
    return {t for t in cjk_bigram(text or "").split() if t}


def _kw_score(query: str, text: str) -> float:
    """词面通道：query bigram 被文本包含的比例（0~1）。"""
    q = _bigrams(query)
    if not q:
        return 0.0
    t = _bigrams(text)
    return len(q & t) / len(q)


def _hours_since(ts: float, now: float) -> float:
    return max(0.0, (now - ts) / 3600.0)


def _cos(a: list[float], b: list[float]) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    try:
        import numpy as np

        va, vb = np.asarray(a, dtype="float32"), np.asarray(b, dtype="float32")
        na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
        if na == 0.0 or nb == 0.0:
            return None
        return float(np.dot(va, vb) / (na * nb))
    except Exception:  # noqa: BLE001 —— 数值层故障转词面通道
        return None


def _min_max(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [1.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _effective_window(ev: dict) -> tuple[float, float]:
    """事件有效窗：t_valid 缺省回落 [ts, +inf)（当前仍为真）。"""
    return (ev.get("t_valid_from") if ev.get("t_valid_from") is not None else ev["ts"],
            ev.get("t_valid_to") if ev.get("t_valid_to") is not None else INF)


def _in_query_window(ev: dict, t_start: float, t_end: float) -> bool:
    """时间窗过滤（r7 评测驱动的语义修正）：
    - 有 t_valid 声明 → **事实有效期**语义：有效窗与查询窗相交即命中
      （"上季度采用的协议"——协议在旧 day 提出、上季度仍有效）；
    - 无 t_valid 声明 → **发生时间**语义：ts 落在查询窗内才命中
      （"最近两周的实验结论"——窗外旧实验不再冒充当前结论；
       [ts, +inf) 与任何窗都相交的旧实现会把窗外旧事件当作命中）。"""
    if ev.get("t_valid_from") is not None or ev.get("t_valid_to") is not None:
        f, t = _effective_window(ev)
        return f < t_end and t > t_start
    return t_start <= ev["ts"] < t_end


def _entity_filter_names(query: str) -> list[str]:
    """query 命中的已知实体名（canonical + 别名，子串匹配）。"""
    from ..db import query as db_query

    names: list[str] = []
    for row in db_query("SELECT canonical_name, aliases_json FROM entities"):
        try:
            aliases = json.loads(row["aliases_json"] or "[]")
        except ValueError:
            aliases = []
        for name in [row["canonical_name"], *aliases]:
            if name and name.strip() and name in query:
                names.append(name)
    return sorted(set(names))


def retrieve_memories(query: str, *, user_id: str = "local", top_k: int = 5,
                      candidate_limit: int = 300, max_hops: int = 2,
                      now_ts: float | None = None,
                      touch: bool = True) -> dict:
    """三信号检索主入口。返回 {"items": [事件+scores], "meta": {...}}；
    每个返回项做 touch_access（recency 信号依据，清单 §三#6）。"""
    from ..db import now as db_now

    now = now_ts if now_ts is not None else db_now()
    cands = [e for e in store.list_events(limit=candidate_limit) if e["user_id"] == user_id]
    meta: dict = {"embed": "off", "time_range": None, "entity_filter": [],
                  "n_candidates": len(cands)}

    # ---- 时间窗（踩坑 #1：自然语言 → 区间 ∩ 有效窗）----
    hit = timeparse.parse_expr(query or "", now=now)
    if hit:
        meta["time_range"] = {"expr": hit.expr, "t_start": hit.t_start, "t_end": hit.t_end}
        cands = [e for e in cands if _in_query_window(e, hit.t_start, hit.t_end)]
        meta["n_candidates"] = len(cands)

    # ---- 实体过滤 ----
    names = _entity_filter_names(query or "")
    if names:
        meta["entity_filter"] = names
        cands = [e for e in cands
                 if any(n in (e.get("entities") or []) or n in (e.get("subject") or "")
                        for n in names)]
        meta["n_candidates"] = len(cands)

    if not cands:
        return {"items": [], "meta": meta}

    # ---- 三信号 ----
    qvec: list[float] | None = None
    try:
        qvec = _query_vector(query or "")
    except Exception:  # noqa: BLE001 —— 查询侧向量失败转词面通道
        qvec = None
    dense = False
    rec_raw, imp_raw, rel_raw = [], [], []
    for e in cands:
        rec_raw.append(RECENCY_DECAY ** _hours_since(
            e["last_access"] if e["last_access"] > 0 else e["ts"], now))
        imp_raw.append(float(e["importance"]))
        rel: float | None = None
        blob = e.get("vec")
        if qvec and blob:
            try:
                c = _cos(qvec, _unpack(blob).tolist())
                if c is not None:
                    rel = max(0.0, c)
                    dense = True
            except Exception:  # noqa: BLE001
                rel = None
        if rel is None:
            rel = _kw_score(query or "", f"{e['content']} {e.get('subject') or ''}")
        rel_raw.append(rel)
    meta["embed"] = "dense" if dense else "off"

    s_rec = _min_max(rec_raw)
    s_imp = _min_max(imp_raw)
    # relevance 特例：所有候选原始相关度全零（词面/向量均无命中）时归一化置零而非抬成 1.0
    # ——否则「无相关记忆」与「完全相关」在排序上不可分，注入层也无法设命中门槛
    s_rel = _min_max(rel_raw) if max(rel_raw, default=0.0) > 1e-9 else [0.0] * len(rel_raw)
    # 三信号权重（r10：默认等权 = Generative Agents 口径；意图自适应按问题类型覆盖）
    w_sum = (settings.ret_w_recency + settings.ret_w_importance + settings.ret_w_relevance)
    scored: list[dict] = []
    for i, e in enumerate(cands):
        base = (settings.ret_w_recency * s_rec[i] + settings.ret_w_importance * s_imp[i]
                + settings.ret_w_relevance * s_rel[i]) / w_sum
        final = base * float(e["strength"])             # FadeMem 强度乘子（只影响排序）
        # 「是否仍为真」一等公民（清单 §三#6 / 陈述模块三）：superseded/refuted 的记忆
        # 当前不为真 → 无时间窗查询时降权；历史不删除，按时间窗查询仍可召回
        stale = e["status"] in ("superseded", "refuted")
        if stale:
            final *= settings.ret_stale_penalty
        item = {k: v for k, v in e.items() if k != "vec"}
        item["scores"] = {"recency": round(s_rec[i], 4), "importance": round(s_imp[i], 4),
                          "relevance": round(s_rel[i], 4),
                          "relevance_raw": round(rel_raw[i], 4),
                          "base": round(base, 4),
                          "strength": round(float(e["strength"]), 4),
                          "final": round(final, 4), "hops": 0, "via": [],
                          "stale_penalized": stale}
        scored.append(item)
    scored.sort(key=lambda x: x["scores"]["final"], reverse=True)
    seeds = scored[:top_k]

    # ---- 多跳 BFS（深度≤2，邻居 ×0.5^hop）----
    if max_hops > 0 and seeds:
        edges = store.list_edges(status="active")
        adj: dict[str, list[tuple[str, dict]]] = {}
        for ed in edges:
            adj.setdefault(ed["src"], []).append((ed["dst"], ed))
            adj.setdefault(ed["dst"], []).append((ed["src"], ed))
        frontier = [(s, 0) for s in seeds]
        idx = {s["id"]: s for s in seeds}          # 去重 + 查表（种子与扩展共用）
        expanded: list[dict] = []
        while frontier:
            node, hop = frontier.pop(0)
            if hop >= max_hops:
                continue
            for nid, _edge in adj.get(node["id"], []):
                if nid in idx:
                    continue
                nb = _event_by_id(nid, user_id)
                if not nb:
                    continue
                item = {k: v for k, v in nb.items() if k != "vec"}
                factor = settings.ret_hop_penalty ** (hop + 1)
                item["scores"] = {"base": node["scores"]["base"], "recency": 0.0,
                                  "importance": 0.0, "relevance": 0.0,
                                  "strength": round(float(nb["strength"]), 4),
                                  "final": round(node["scores"]["final"] * factor, 4),
                                  "hops": hop + 1,
                                  "via": node["scores"]["via"] + [node["id"]]}
                idx[nid] = item
                expanded.append(item)
                frontier.append((item, hop + 1))
        seeds.extend(expanded)
        seeds.sort(key=lambda x: x["scores"]["final"], reverse=True)

    if touch:
        for s in seeds:
            try:
                store.touch_access(s["id"])
            except Exception:  # noqa: BLE001, S110 —— 计数失败不影响检索结果
                pass
    return {"items": seeds[: max(top_k * 3, top_k)], "meta": meta}


def _event_by_id(event_id: str, user_id: str) -> dict | None:
    ev = store.get_event(event_id)
    return ev if ev and ev["user_id"] == user_id else None


def pack_vec(vec: list[float]) -> bytes:
    """对外薄封装：记忆向量入库统一 float32 LE（与 chunks.vec 同约定）。"""
    return _pack(vec)


def hours_since_label(ts: float, now: float | None = None) -> str:
    """人读的时间距离（注入 responder 时间线块时用）。"""
    n = now if now is not None else datetime.now(UTC).timestamp()
    d = max(0.0, n - ts)
    if d < 3600:
        return "刚刚"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    if d < 30 * 86400:
        return f"{int(d // 86400)} 天前"
    return f"{int(d // (30 * 86400))} 个月前"


# responder 注入的 kind 过滤（清单 §三#12）：专业模式只注入研究性记忆，普通模式全量
PAPER_MODE_KINDS = frozenset({"hypothesis", "finding", "decision"})


def build_memory_block(query: str, *, doc_mode: str = "paper", user_id: str = "local",
                       top_k: int = 5, max_events: int = 8,
                       now_ts: float | None = None) -> tuple[str, dict, dict]:
    """检索 + 时间线路径 → responder 注入块（[M#] 动态编号）与回放锚点映射。

    r10 扫参最优默认：top_k=5 / max_events=8（注入时间线 3 条不够盘点与历史覆盖，
    42 组网格实测 avg 0.92 vs 3 条 0.845）。返回 ("", {}) 当无相关记忆或任何内部
    失败——**注入绝不阻断问答主链路**。M# 编号空间与 Evidence Pack 的 E# 完全独立
    （清单 §2.9），锚点携带 conversation_id/turn/thread_id 供前端点击回放。"""
    try:
        res = retrieve_memories(query or "", user_id=user_id, top_k=top_k,
                                now_ts=now_ts, touch=True)
        allowed = None if doc_mode == "general" else PAPER_MODE_KINDS
        # 命中门槛看**原始**相关度（归一化会把弱相关压成 0）：无词面/向量命中的记忆不注入
        seeds = [i for i in res["items"] if i["scores"]["hops"] == 0
                 and i["scores"]["relevance_raw"] > 0
                 and (allowed is None or i["kind"] in allowed)]
        gate = memory_qa.gate_from_ranked(res["items"])
        if not seeds:
            return "", {}, gate
        from . import graph as _graph  # 惰性导入避免 retrieve↔graph 循环

        path = _graph.timeline_path([s["id"] for s in seeds], max_events=max_events)
        events = [e for e in path["events"] if allowed is None or e["kind"] in allowed]
        if not events:
            return "", {}, gate
        lines: list[str] = []
        anchors: dict[str, dict] = {}
        for i, e in enumerate(events, 1):
            when = hours_since_label(e["ts"], now_ts)
            src = f" · 会话 {e['conversation_id'][:8]} 第 {e['turn']} 轮" \
                if e["conversation_id"] else ""
            lines.append(f"[M{i}] ({e['kind']} · {when}{src}) {e['content']}")
            anchors[f"M{i}"] = {"memory_id": e["id"], "kind": e["kind"],
                                "status": e.get("status", ""),
                                "conversation_id": e["conversation_id"],
                                "turn": e["turn"], "thread_id": e["thread_id"],
                                "content": e["content"], "ts": e["ts"]}
        block = (
            "以下是该用户本人的历史研究记忆（按时间线，[M#] 编号）。"
            "当用户问题涉及其历史研究（过去的假设/实验/决定/偏好及其现状）时，"
            "**应以本时间线为回答依据并引用对应 [M#]**——这是合法的回答来源，"
            "不属编造；论文内容仍只以 [E#] 证据为准，两者不得混用：\n" + "\n".join(lines))
        return block, anchors, gate
    except Exception as e:  # noqa: BLE001 —— 记忆注入失败不影响回答
        import logging

        logging.getLogger("meridian.memory").warning(
            "build_memory_block 失败（已跳过注入）: %s", type(e).__name__)
        return "", {}, {"trigger": False, "top_relevance_raw": 0.0,
                        "threshold": memory_qa._MEMORY_QA_TOP_REL}
