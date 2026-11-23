"""时间知识图谱构建（模块七 R3/P1，清单 §三#5）：THEANINE Phase I 建边 + Phase II 时间线路径。

候选 = 同用户更早事件按相似度 top_n；
相似度：双方有向量 → 余弦；否则 cjk bigram Jaccard（词面兜底，evidence.engine 如实标注）。
关系判定两路（清单 §一 内部替换②b）：
- use_llm=False（规则版，kind=rule）：反驳标记→refutes（新→旧）/ 取代标记→supersedes
  （新→旧）/ 相似度≥阈值→same_topic（旧→新，时序先序）；
- use_llm=True（P1 升级，kind=llm）：memory_relations.yaml LLM 判六类关系或 none；
  离线/失败自动回落规则路。**同事件对上关系不同的旧边（含规则边）标 superseded
  保留演化史**，不删除——append-only 与事件流同哲学。
方向约定：refutes/supersedes = 新→旧（语义指向被作用方）；其余 = 旧→新。
"""
from __future__ import annotations

import itertools
import json
import logging
import re

from ..llm import client as llm
from ..llm import prompts
from . import store

log = logging.getLogger("meridian.memory")

TOP_N = 3
SAME_TOPIC_THRESHOLD = 0.30

# 「与」单独太弱：仅当同句出现 矛盾/不一致 时才算反驳标记（组合判断见 _has_refutes_mark）
_REFUTES_CO = ("矛盾", "不一致", "相反")
_SUPERSEDE_MARK = ("取代", "替代", "supersede", "不再成立", "修正了", "修正假设")

# LLM 关系语义方向：指向被作用方（新→旧）；其余关系沿时序先序（旧→新）
_NEW_TO_OLD = {"refutes", "supersedes"}

# LLM 兜底 prompt（memory_relations.yaml 缺失时 resolve 回退，version='builtin'）
_BUILTIN_REL = (
    "判定两条研究记忆之间的关系。只输出 JSON："
    '{"relation":"refutes|supersedes|supports|elaborates|same_topic|cause|none"}。'
    "refutes=后者推翻前者结论；supersedes=后者取代前者；supports=后者支持前者；"
    "elaborates=后者细化前者；same_topic=同主题并列；cause=后者是前者的动机/原因；"
    "证据不足判 none，绝不猜。")


def _bigrams(text: str) -> set[str]:
    from .retrieve import _bigrams as _b
    return _b(text)


def _similarity(a: dict, b: dict) -> tuple[float, str]:
    """(相似度, 引擎标注)。有向量走余弦；否则词面 Jaccard。"""
    va, vb = a.get("vec"), b.get("vec")
    if va and vb:
        from .retrieve import _cos, _unpack

        try:
            c = _cos(_unpack(va).tolist(), _unpack(vb).tolist())
            if c is not None:
                return max(0.0, c), "dense"
        except Exception as e:  # noqa: BLE001 —— 向量故障转词面
            log.warning("记忆相似度向量故障转词面: %s", type(e).__name__)
    ba, bb = _bigrams(f"{a['content']} {a.get('subject') or ''}"), \
        _bigrams(f"{b['content']} {b.get('subject') or ''}")
    union = ba | bb
    if not union:
        return 0.0, "lexical"
    return len(ba & bb) / len(union), "lexical"


def _has_refutes_mark(text: str) -> bool:
    low = text.lower()
    if any(m in low for m in ("推翻", "反驳", "不成立", "被证伪", "refute", "contradict")):
        return True
    return ("与" in low or "跟" in low) and any(c in low for c in _REFUTES_CO)


def _has_supersede_mark(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _SUPERSEDE_MARK)


def _judge_relation_llm(new_text: str, old_text: str) -> str | None:
    """LLM 判关系（P1 升级）：六类关系或 none（不可判）；离线/失败/非法 → None 走规则路。"""
    if not llm.available():
        return None
    text, version = prompts.resolve("memory_relations", "system_relations", _BUILTIN_REL)
    try:
        res = llm.chat(
            [{"role": "system", "content": text},
             {"role": "user", "content":
              f"<newer>{new_text[:300]}</newer>\n<older>{old_text[:300]}</older>"}],
            json_mode=True, max_tokens=60, module="memory_relations",
            audit_prompt=new_text[:100], prompt_version=version)
    except Exception as e:  # noqa: BLE001 —— LLM 失败回落规则路
        log.warning("memory_relations LLM 失败 → 规则路: %s", e)
        return None
    if not res.ok:
        return None
    try:
        m = re.search(r"\{.*\}", res.get("text") or "", re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except (ValueError, TypeError):
        return None
    rel = data.get("relation")
    return rel if rel in store.RELATIONS else None


def _supersede_pair_edges(a_id: str, b_id: str, keep_relation: str) -> int:
    """同事件对上关系不同的 active 边（含规则边）标 superseded——保留演化史不删除。"""
    n = 0
    for ed in store.list_edges(status="active"):
        if {ed["src"], ed["dst"]} == {a_id, b_id} and ed["relation"] != keep_relation:
            evidence = ed["evidence"]
            evidence["superseded_by_relation"] = keep_relation
            from ..db import tx

            with tx() as c:
                c.execute("UPDATE memory_edges SET status='superseded', evidence_json=? "
                          "WHERE id=?", (json.dumps(evidence, ensure_ascii=False), ed["id"]))
            n += 1
    return n


def link_event(event_id: str, *, user_id: str = "local", top_n: int = TOP_N,
               threshold: float = SAME_TOPIC_THRESHOLD,
               use_llm: bool = False) -> list[dict]:
    """为一条记忆事件建边（Phase I）。候选=同用户更早事件 top_n 相似。
    use_llm=True 时 LLM 判关系（kind=llm，离线自动回落规则路）。"""
    ev = store.get_event(event_id)
    if not ev or ev["user_id"] != user_id:
        return []
    # list_events 无 user 过滤（R1 签名），本地过滤：
    older = [e for e in store.list_events(limit=500)
             if e["user_id"] == user_id and e["id"] != event_id and e["ts"] <= ev["ts"]]
    if not older:
        return []
    scored = []
    for cand in older:
        sim, engine = _similarity(ev, cand)
        scored.append((sim, engine, cand))
    scored.sort(key=lambda t: t[0], reverse=True)
    created: list[dict] = []
    for sim, engine, cand in scored[:top_n]:
        evidence = {"sim": round(sim, 4), "engine": engine}
        rel, kind, src, dst = None, "rule", None, None
        if use_llm:
            rel = _judge_relation_llm(
                f"{ev['content']} {ev.get('subject') or ''}",
                f"{cand['content']} {cand.get('subject') or ''}")
            if rel is not None:
                kind = "llm"
                src, dst = (ev["id"], cand["id"]) if rel in _NEW_TO_OLD \
                    else (cand["id"], ev["id"])
        if rel is None:
            # 规则路（kind=rule）：语义标记优先，其次相似度阈值
            if sim < 0.12 and not (_has_refutes_mark(ev["content"]) or
                                   _has_supersede_mark(ev["content"])):
                continue  # 弱相关且无语义标记 → 不建边
            if cand["kind"] == "hypothesis" and _has_supersede_mark(ev["content"]):
                rel, src, dst = "supersedes", ev["id"], cand["id"]
            elif cand["kind"] == "hypothesis" and _has_refutes_mark(ev["content"]):
                rel, src, dst = "refutes", ev["id"], cand["id"]
            elif sim >= threshold:
                rel, src, dst = "same_topic", cand["id"], ev["id"]
            else:
                continue
        # LLM 关系与该事件对上现有 active 边不同 → 旧边（含规则边）superseded 保留演化史
        if kind == "llm":
            _supersede_pair_edges(ev["id"], cand["id"], rel)
        created.append(store.upsert_edge(src, dst, rel, kind=kind,
                                         evidence=evidence, verdict_source=kind))
    return created


def build_for_conversation(conversation_id: str, *, user_id: str = "local") -> list[dict]:
    """按时间序为整段会话补建边（worker/演示用；幂等，UPSERT 语义）。"""
    events = [e for e in store.list_events(conversation_id=conversation_id, limit=200)
              if e["user_id"] == user_id]
    events.sort(key=lambda e: e["ts"])
    out: list[dict] = []
    for e in events:
        out.extend(link_event(e["id"], user_id=user_id))
    return out


def timeline_path(seed_ids: list[str], *, max_events: int = 12) -> dict:
    """Phase II：从种子沿 active 边（无向）取连通分量，按 ts 线性化成时间线路径。
    返回 {"events": [事件]，"links": [相邻事件间的边 relation]}——THEANINE 的
    「事件 A → 因为 X → 导致事件 B」线性投影。"""
    events: dict[str, dict] = {}
    frontier = [s for s in seed_ids if s]
    while frontier and len(events) < max_events:
        eid = frontier.pop(0)
        if eid in events:
            continue
        ev = store.get_event(eid)
        if not ev:
            continue
        events[eid] = ev
        for ed in store.list_edges(src=eid, status="active") + \
                store.list_edges(dst=eid, status="active"):
            other = ed["dst"] if ed["src"] == eid else ed["src"]
            if other not in events:
                frontier.append(other)
    ordered = sorted(events.values(), key=lambda e: e["ts"])[:max_events]
    links: list[dict] = []
    for a, b in itertools.pairwise(ordered):
        rel = ""
        for ed in store.list_edges(src=a["id"], dst=b["id"], status="active") + \
                store.list_edges(src=b["id"], dst=a["id"], status="active"):
            rel = ed["relation"]
            break
        links.append({"src": a["id"], "dst": b["id"], "relation": rel})
    return {"events": ordered, "links": links}
