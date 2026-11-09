"""图谱挖掘与学术洞察（4.6，创新分最高项）。

四个能力，全部可降级、可单测：
- enrich(graph)：louvain 社区 + 时间序位 + 基石/边缘角色判定（规则可解释）
- classify_citations(paper_ids)：引用含金量（substantive/background/perfunctory → 1.0/0.4/0.1），
  规则兜底 + LLM 批量增强；边权重 = Σ被引含金量
- community_summary(paper_ids, community)：top3 论文 → Evidence Pack [E#] → LLM 三阶段演进综述
  → 复用引用后验 + ClaimTrace（洞察也抗幻觉——别的队做不到的点）
- future_works(paper_ids)：Conclusion/Limitation 抽想法 → 反向引用找后续 → 逐条判定
  solved/partial/open；无后续引用的 idea 直接 open（逻辑必然，非 LLM 猜测）
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

from .. import db
from ..config import DOCIR_DIR, settings
from ..llm import client as llm
from ..llm.prompts import resolve as _resolve
from ..llm.schemas import CitationClassList, FutureCheckList, FutureWorkList, IdeaVerdictList
from ..log import logger
from ..rag.answering import validate_citations
from ..rag.indexing import hybrid_search
from ..rag.verify import verify_claims
from .tools import _citation_graph, _graph_metrics

_YEAR_RE = re.compile(r"^(\d{2})(\d{2})\.")
_CITE_MARK_RE = re.compile(r"\[(\d{1,3})\]")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])|。\s*")

# 含金量权重（答辩口径：实质性 1.0 / 背景 0.4 / 凑数 0.1）
_CLASS_WEIGHT = {"substantive": 1.0, "background": 0.4, "perfunctory": 0.1}


def _load_docir(paper_id: str) -> dict:
    path = Path(DOCIR_DIR) / f"{paper_id}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— DocIR 损坏视同无文档
        return {}


def _year_of(paper: dict) -> int | None:
    """arXiv id 前四位（YYMM）→ 年份（1706→2017, 2401→2024）；无 arXiv 用 created_at。"""
    m = _YEAR_RE.match(paper.get("arxiv_id") or "")
    if m:
        return 2000 + int(m.group(1))   # YY（前两位）为年份后两位，MM 为月份
    created = paper.get("created_at")
    if created:
        import datetime
        return datetime.datetime.fromtimestamp(created).year
    return None


# ---------- 图谱增强：社区 / 时间 / 角色 ----------

def enrich(graph: dict) -> dict:
    """在 _citation_graph 产物上补 community/year/time_rank/role/modularity。"""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    papers = {r["id"]: dict(r) for r in db.query("SELECT id,title,arxiv_id,created_at FROM papers WHERE status='active'")}
    years = {n["id"]: _year_of(papers.get(n["id"], {})) for n in nodes}

    communities: dict[str, int] = {}
    modularity = None
    try:
        import networkx as nx
        g = nx.Graph()
        g.add_nodes_from(n["id"] for n in nodes)
        g.add_edges_from((e["source"], e["target"]) for e in edges)
        if g.number_of_nodes() and g.number_of_edges():
            cs = nx.community.louvain_communities(g, seed=42)
            comm_of = {}
            for cid, members in enumerate(sorted(cs, key=lambda s: -len(s))):
                for pid in members:
                    comm_of[pid] = cid
            communities = comm_of
            modularity = round(nx.community.modularity(g, cs), 4)
    except Exception as e:  # noqa: BLE001 —— 社区划分失败 → 全体同一社区（如实降级）
        logger.warning(f"louvain 失败 → 单社区降级: {type(e).__name__}: {e}", module="insights")
        communities = {n["id"]: 0 for n in nodes}

    # 社区统计：中位年 / 邻居跨社区比例
    members_by_comm: dict[int, list[str]] = {}
    for pid, cid in communities.items():
        members_by_comm.setdefault(cid, []).append(pid)
    median_year = {}
    for cid, pids in members_by_comm.items():
        ys = [years[p] for p in pids if years[p] is not None]
        median_year[cid] = statistics.median(ys) if ys else None
    neighbors: dict[str, set[str]] = {}
    cross_ratio: dict[str, float] = {}
    for e in edges:
        neighbors.setdefault(e["source"], set()).add(e["target"])
        neighbors.setdefault(e["target"], set()).add(e["source"])
    for pid, nb in neighbors.items():
        my_c = communities.get(pid)
        cross = sum(1 for x in nb if communities.get(x) != my_c)
        cross_ratio[pid] = cross / len(nb) if nb else 0.0

    # 指标（复用 _graph_metrics，PageRank/介数）
    metrics = _graph_metrics(graph)
    pr_by_pid = {m["paper_id"]: m for m in metrics.get("nodes", [])}
    pr_vals = [m["pagerank"] for m in metrics.get("nodes", [])]
    in_vals = [m["in_degree"] for m in metrics.get("nodes", [])]
    p70_pr = _percentile(pr_vals, 0.70)
    p70_in = _percentile(in_vals, 0.70)
    p30_in = _percentile(in_vals, 0.30)

    out_nodes = []
    for m in metrics.get("nodes", []):
        pid = m["paper_id"]
        cid = communities.get(pid, 0)
        year = years.get(pid)
        role = ""
        role_reason = ""
        med = median_year.get(cid)
        if med is not None and year is not None:
            if year <= med and m["pagerank"] >= p70_pr and m["in_degree"] >= p70_in:
                role = "cornerstone"     # 基石论文：早期 + 高 PageRank + 高被引
                role_reason = (f"{year} 年发表（早于社区中位年 {med}）· "
                               f"PageRank {m['pagerank']:.3f} ≥ P70({p70_pr:.3f}) · "
                               f"被引 {m['in_degree']} ≥ P70({p70_in}) → 基石")
            elif year > med and m["in_degree"] <= p30_in and cross_ratio.get(pid, 0) >= 0.5:
                role = "peripheral"      # 边缘衍生：新 + 低被引 + 跨社区桥接
                role_reason = (f"{year} 年发表（晚于社区中位年 {med}）· "
                               f"被引 {m['in_degree']} ≤ P30({p30_in}) · "
                               f"跨社区邻居占比 {cross_ratio.get(pid, 0):.0%} → 边缘衍生")
        out_nodes.append({**m, "year": year, "community": cid, "role": role,
                          "role_reason": role_reason,
                          "time_rank": _time_rank(pid, members_by_comm.get(cid, []), years)})

    return {
        "backend": metrics.get("backend", "none"),
        "edge_count": len(edges),
        "nodes": out_nodes,
        "edges": edges,
        "external_count": graph.get("external_count", 0),
        "communities": [{"id": cid, "size": len(pids),
                         "top_paper": max(pids, key=lambda p: pr_by_pid.get(p, {}).get("pagerank", 0),
                                          default=None)}
                        for cid, pids in sorted(members_by_comm.items())],
        "modularity": modularity,
    }


def _percentile(vals: list, q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(q * (len(s) - 1))))
    return s[idx]


def _time_rank(pid: str, members: list[str], years: dict) -> float | None:
    """社区内按年排序的百分位（0=最早/奠基，1=最新）。"""
    ys = sorted(years[p] for p in members if years[p] is not None)
    y = years.get(pid)
    if y is None or not ys:
        return None
    return round(sum(1 for x in ys if x <= y) / len(ys), 3)


# ---------- 引用含金量 ----------

def _rule_class(sentence: str, section: str) -> str:
    """规则兜底：Methods/Results → 实质；Introduction/Related → 背景；其余 → 凑数。"""
    s = (section or "").lower()
    if any(k in s for k in ("method", "experiment", "result", "model", "approach")):
        return "substantive"
    if any(k in s for k in ("introduction", "related", "background")):
        return "background"
    return "perfunctory"


def classify_citations(paper_ids: list[str]) -> dict:
    """扫描每篇论文正文中的 [n] 标记 → 句子级分类 → 边含金量加权。

    产出：citations[{paper_id, n, target, mentions:[{sentence, section, cls, w}]}]、
    edge_weights{"src|dst": Σw}、class_stat 计数。LLM 可用时批量增强分类（失败走规则）。
    """
    graph = _citation_graph(paper_ids)
    # ref idx → 库内目标（与 _citation_graph 相同的匹配规则）
    papers = {r["id"]: dict(r) for r in db.query("SELECT id,title,arxiv_id FROM papers WHERE status='active'")}
    by_arxiv = {p["arxiv_id"]: pid for pid, p in papers.items() if p.get("arxiv_id")}
    by_title = {p["title"].strip().lower(): pid for pid, p in papers.items() if p.get("title")}

    mentions: list[dict] = []       # [{key:"pid|n", sentence, section}]
    for pid in paper_ids:
        docir = _load_docir(pid)
        for b in docir.get("blocks", []):
            if b.get("type") not in ("paragraph", "heading"):
                continue
            text = b.get("text") or ""
            if not _CITE_MARK_RE.search(text):
                continue
            for sent in _SENT_SPLIT_RE.split(text):
                for m in _CITE_MARK_RE.finditer(sent):
                    mentions.append({"key": f"{pid}|{int(m.group(1))}",
                                     "sentence": sent.strip()[:200],
                                     "section": b.get("section_path") or ""})

    # LLM 批量增强（可选；输入编号即 mentions 下标）
    cls_by_idx: dict[int, str] = {}
    if mentions and llm.available() and settings.semantic_judge:
        try:
            system, pv = _resolve("insights", "system_classify",
                                  "你是引用含金量分析器。输出 JSON 分类。")
            lines = [f'{{"id":{i},"sentence":{json.dumps(m["sentence"], ensure_ascii=False)},'
                     f'"section":{json.dumps(m["section"], ensure_ascii=False)}}}'
                     for i, m in enumerate(mentions[:60])]
            data = llm.chat_json_validated(
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"<mentions>\n[{', '.join(lines)}]\n</mentions>"}],
                CitationClassList, retries=1, module="cite_classify",
                audit_prompt=mentions[0]["sentence"][:200], prompt_version=pv,
                temperature=0.0)
            for c in (data or {}).get("classes", []):
                if c.get("cls") in _CLASS_WEIGHT:
                    cls_by_idx[int(c["idx"])] = c["cls"]
        except Exception as e:  # noqa: BLE001 —— 增强失败走规则
            logger.warning(f"含金量 LLM 增强失败 → 规则兜底: {e}", module="insights")

    # ref idx → target
    ref_target: dict[str, str | None] = {}
    for pid in paper_ids:
        refs = db.query('SELECT idx,title,arxiv_id FROM "references" WHERE paper_id=?', (pid,))
        for ref in refs:
            target = None
            if ref["arxiv_id"] and ref["arxiv_id"] in by_arxiv:
                target = by_arxiv[ref["arxiv_id"]]
            elif ref["title"]:
                m = re.search(r"arXiv:(\d{4}\.\d{4,5})", ref["title"], re.IGNORECASE)
                if m and m.group(1) in by_arxiv:
                    target = by_arxiv[m.group(1)]
                elif ref["title"].strip().lower() in by_title:
                    target = by_title[ref["title"].strip().lower()]
            ref_target[f"{pid}|{ref['idx']}"] = target

    enriched: dict[str, dict] = {}
    edge_weights: dict[str, float] = {}
    class_stat = {"substantive": 0, "background": 0, "perfunctory": 0}
    for i, m in enumerate(mentions):
        cls = cls_by_idx.get(i) or _rule_class(m["sentence"], m["section"])
        w = _CLASS_WEIGHT[cls]
        class_stat[cls] += 1
        target = ref_target.get(m["key"])
        enriched.setdefault(m["key"], {"target": target, "mentions": []})["mentions"].append(
            {"sentence": m["sentence"], "section": m["section"], "cls": cls, "w": w})
        if target:
            src = m["key"].rsplit("|", 1)[0]
            ek = f"{src}|{target}"
            edge_weights[ek] = round(edge_weights.get(ek, 0.0) + w, 2)

    weighted_edges = []
    for e in graph.get("edges", []):
        ek = f"{e['source']}|{e['target']}"
        weight = edge_weights.get(ek, 0.0)
        # 悬停明细：该边上的实质性引用句（最多 2 条）
        examples = []
        for m in mentions:
            if m["key"].rsplit("|", 1)[0] != e["source"]:
                continue
            slot = enriched.get(m["key"])
            if slot and slot.get("target") == e["target"] and slot["mentions"]:
                best = max(slot["mentions"], key=lambda x: x["w"])
                if best["cls"] == "substantive":
                    examples.append(best)
        weighted_edges.append({**e, "weight": weight, "examples": examples[:2]})

    return {"edges": weighted_edges, "edge_weights": edge_weights,
            "class_stat": class_stat, "mention_count": len(mentions),
            "enriched": enriched}


def reference_quality(paper_id: str) -> dict:
    """单篇参考文献含金量（模块四 P0-2）：ref idx → {cls, weight, mentions, examples}。

    供文献下钻/References 面板展示与可逆过滤：无引用句的条目不出现（前端显式
    「无引用句」中性态，不假装判断）。cls 取该条所有引用句中权重最高者的分类。"""
    r = classify_citations([paper_id])
    out: dict[int, dict] = {}
    for key, slot in (r.get("enriched") or {}).items():
        pid, _, n = key.rpartition("|")
        if pid != paper_id or not slot.get("mentions"):
            continue
        ms = slot["mentions"]
        best = max(ms, key=lambda x: x["w"])
        out[int(n)] = {
            "cls": best["cls"],
            "weight": round(sum(m["w"] for m in ms), 2),
            "mention_count": len(ms),
            "examples": [m["sentence"] for m in sorted(ms, key=lambda x: -x["w"])[:2]],
            "target_in_library": bool(slot.get("target")),
        }
    return {"paper_id": paper_id, "qualities": out,
            "class_stat": r.get("class_stat", {}),
            "note": "无引用句的条目不在 qualities 中（中性态，不做判断）"}


# ---------- 社区综述（洞察也抗幻觉） ----------

def community_summary(paper_ids: list[str], community_id: int) -> dict:
    """top3 pagerank 论文 → kb_search 证据 → [E#] 强制引用综述 → L1 后验 + ClaimTrace。"""
    g = enrich(_citation_graph(paper_ids))
    members = [n for n in g["nodes"] if n["community"] == community_id]
    if not members:
        return {"error": f"community_{community_id}_not_found"}
    top3 = [m["paper_id"] for m in sorted(members, key=lambda x: -x["pagerank"])[:3]]

    hits: list[dict] = []
    for pid in top3:
        title = next((m["title"] for m in members if m["paper_id"] == pid), "")
        r = hybrid_search([pid], title or "main contribution", top_k=4)
        hits.extend(r.get("hits", [])[:4])

    # Evidence Pack 编号制（复用 answering.format_context）
    from ..rag.answering import format_context
    context, eid_map = format_context(hits)

    system, pv = _resolve("insights", "system_community",
                          "你是学术脉络综述作者。每句带 [E#] 引用；证据不足如实说明。")
    years = sorted([m["year"] for m in members if m["year"] is not None])
    res = llm.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content":
             f"<community>\n论文数 {len(members)}，年份跨度 {years[0] if years else '?'}-"
             f"{years[-1] if years else '?'}，代表作（PageRank top3）："
             f"{[m['title'][:40] for m in sorted(members, key=lambda x: -x['pagerank'])[:3]]}\n"
             f"</community>\n\n<context>\n{context[:8000]}\n</context>\n\n请写三阶段演进综述。"}],
        temperature=0.2, max_tokens=800, module="community_summary",
        audit_prompt=f"community {community_id}", prompt_version=pv)
    answer = res.get("text", "")
    guardrails: list[str] = []
    if not answer:
        return {"summary": "（LLM 不可用，无法生成综述）", "claims": [], "anchors": {},
                "guardrails": ["llm_unavailable_fallback"]}
    answer, g1 = validate_citations(answer, eid_map)
    guardrails += g1
    claims, _ = llm_claims_safe(answer, hits)
    claims, g2 = verify_claims(claims, eid_map)
    guardrails += g2
    from ..rag.answering import build_anchors
    return {"summary": answer, "claims": claims,
            "anchors": build_anchors(eid_map), "guardrails": guardrails}


def llm_claims_safe(answer: str, hits: list[dict]) -> tuple[list[dict], dict[int, dict]]:
    from ..rag.answering import llm_claims
    try:
        return llm_claims(answer, hits)
    except Exception:  # noqa: BLE001 —— ClaimTrace 失败不阻塞综述交付
        return [], {}


# ---------- Future Work 去重过滤 ----------

_FUTURE_KEYWORDS = ("future work", "future direction", "further", "future research",
                    "future study", "we plan", "we leave", "remains", "remains to be")
_FUTURE_ZH = ("未来工作", "后续研究", "进一步", "有待")

_TAIL_SECTIONS = ("conclusion", "limitation", "discussion", "future", "concluding")


def _extract_future_ideas(paper_ids: list[str]) -> list[dict]:
    """尾章抽取：规则（future 关键句）+ LLM 批量概括（失败逐句直用）。"""
    ideas: list[dict] = []
    for pid in paper_ids:
        docir = _load_docir(pid)
        tail_blocks = [b for b in docir.get("blocks", [])
                       if b.get("type") == "paragraph"
                       and any(k in (b.get("section_path") or "").lower()
                               for k in _TAIL_SECTIONS)]
        if not tail_blocks:
            blocks = docir.get("blocks", [])
            tail_blocks = blocks[-6:] if len(blocks) > 6 else blocks
        texts = [(b.get("text") or "") for b in tail_blocks]
        cand: list[str] = []
        for text in texts:
            for sent in re.split(r"(?<=[.!?])\s+|。", text):
                low = sent.lower()
                if len(sent.strip()) > 20 and (any(k in low for k in _FUTURE_KEYWORDS)
                                               or any(k in sent for k in _FUTURE_ZH)):
                    cand.append(sent.strip()[:300])
        if cand and llm.available():
            try:
                system, pv = _resolve("insights", "system_future_extract",
                                      "你是研究机会抽取器。输出 JSON ideas。")
                data = llm.chat_json_validated(
                    [{"role": "system", "content": system},
                     {"role": "user", "content":
                         f"<passages>\n{chr(10).join('- ' + c for c in cand[:12])}\n</passages>"}],
                    FutureWorkList, retries=1, module="future_extract",
                    audit_prompt=cand[0][:200], prompt_version=pv, temperature=0.0)
                got = [i["idea_text"] for i in (data or {}).get("ideas", []) if i.get("idea_text")]
                if got:
                    ideas.extend({"idea_text": t, "source_paper": pid,
                                  "anchor": _anchor_of(docir, cand[0])} for t in got[:5])
                    continue
            except Exception as e:  # noqa: BLE001 —— LLM 概括失败 → 直用原句
                logger.warning(f"future 概括失败 → 原句直用: {e}", module="insights")
        ideas.extend({"idea_text": c, "source_paper": pid,
                      "anchor": _anchor_of(docir, c)} for c in cand[:6])
    return ideas


def _anchor_of(docir: dict, sentence: str) -> dict:
    """句子 → {page, bbox} 锚点（命中块的首个匹配；扫描件 bbox 为全页占位）。"""
    low = sentence[:60].lower()
    for b in docir.get("blocks", []):
        if low and low in (b.get("text") or "").lower():
            return {"page": (b.get("page") or 0) + 1, "bbox": b.get("bbox") or [],
                    "section_path": b.get("section_path") or ""}
    return {"page": None, "bbox": [], "section_path": ""}


def future_works(paper_ids: list[str]) -> dict:
    """抽取 → 反向引用找后续 → 逐条判定 solved/partial/open。

    无后续论文引用的 idea 直接 open（没人跟进 = 缝隙还在，逻辑必然而非 LLM 猜测）。
    """
    ideas = _extract_future_ideas(paper_ids)
    graph = _citation_graph(paper_ids)
    successors: dict[str, list[str]] = {}
    for e in graph.get("edges", []):
        successors.setdefault(e["target"], []).append(e["source"])   # 反边：谁引用了它
    # 被引数（升序=越没人做越靠前）
    cited_count = {pid: len({s for s in v}) for pid, v in successors.items()}

    out: list[dict] = []
    pending: list[int] = []          # 需 LLM 判定的下标
    for i, idea in enumerate(ideas):
        src = idea["source_paper"]
        succ = list(dict.fromkeys(successors.get(src, [])))
        item = {**idea, "id": i, "successors": succ, "cited_count": cited_count.get(src, 0),
                "status": "open", "reason": "", "evidence": []}
        if succ:
            # 后续论文检索相关段落
            query = idea["idea_text"][:120]
            r = hybrid_search(succ[:4], query, top_k=3)
            item["evidence"] = [{"paper_id": h.get("paper_id"),
                                 "page": (h.get("page") or 0) + 1,
                                 "text": (h.get("text") or "")[:200]}
                                for h in r.get("hits", [])]
            pending.append(i)
        else:
            item["reason"] = "无库内后续论文引用该方向 → 缝隙仍在"
        out.append(item)

    # LLM 批量判定（有后续证据的）
    if pending and llm.available():
        try:
            system, pv = _resolve("insights", "system_future_judge",
                                  "你是研究缝隙判定器。输出 JSON verdicts。")
            lines = []
            for i in pending:
                ev = "；".join(f"[{e['paper_id'][:8]}] {e['text'][:120]}"
                               for e in out[i]["evidence"]) or "（无可依据段落）"
                lines.append(f'{{"id":{i},"idea":{json.dumps(out[i]["idea_text"], ensure_ascii=False)},'
                             f'"followup":{json.dumps(ev, ensure_ascii=False)}}}')
            data = llm.chat_json_validated(
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"<ideas>\n[{', '.join(lines)}]\n</ideas>"}],
                IdeaVerdictList, retries=1, module="future_judge",
                audit_prompt=out[pending[0]]["idea_text"][:200], prompt_version=pv,
                temperature=0.0)
            by_id = {int(v["id"]): v for v in (data or {}).get("verdicts", [])
                     if v.get("verdict") in ("solved", "partial", "open")}
            for i in pending:
                v = by_id.get(i)
                if v:
                    out[i]["status"] = v["verdict"]
                    out[i]["reason"] = v.get("reason", "")
        except Exception as e:  # noqa: BLE001 —— 判定失败保持 open + 如实 reason
            logger.warning(f"future 判定失败 → 全部保持 open: {e}", module="insights")
            for i in pending:
                out[i]["reason"] = "（判定不可用，保守标记 open）"

    solved = [x for x in out if x["status"] in ("solved", "partial")]
    opened = sorted([x for x in out if x["status"] == "open"],
                    key=lambda x: x["cited_count"])   # 越没人做越靠前
    return {"solved": solved, "open": opened, "total": len(out)}


# ---------- 模块五 P0-2：整批领域综述 ----------

_SURVEY_SECTIONS = ["背景与问题", "时间线与演进", "方法族（社区）", "基石与边缘", "开放问题"]


def _evidence_pack(paper_ids: list[str], per_paper: int = 2) -> tuple[list[dict], dict[int, dict]]:
    """批量证据池：每篇论文取其最有代表性的段落（标题+首段检索），编号 [E#]。"""
    from ..rag.answering import format_context

    hits: list[dict] = []
    titles: dict[str, str] = {}
    for pid in paper_ids:
        rows = db.query("SELECT title FROM papers WHERE id=?", (pid,))
        title = (rows[0]["title"] if rows else "") or pid
        titles[pid] = title
        r = hybrid_search([pid], title or "main contribution method", top_k=per_paper)
        for h in r.get("hits", []):
            h = dict(h)
            h["_paper_title"] = title
            hits.append(h)
    context, eid_map = format_context(hits)
    return hits, {"context": context, "eid_map": eid_map, "titles": titles}


def survey(paper_ids: list[str], *, progress=None) -> dict:
    """整批领域综述（模块五 P0-2）：五节结构，每节 Evidence Pack + 完整抗幻觉链。

    结构来自图谱客观信号（年份跨度/社区/角色），生成走 LLM；每节独立
    validate_citations → llm_claims → verify_claims（与社区综述同链）。
    LLM 不可用 → 结构化骨架 + 图谱客观数据如实交付（不冒充综述正文）。"""
    if len(paper_ids) < 2:
        return {"error": "survey_needs_2_papers"}
    graph = enrich(_citation_graph(paper_ids))
    nodes = graph.get("nodes", [])
    communities = graph.get("communities", [])
    years = sorted({n.get("year") for n in nodes if n.get("year")})
    cornerstones = [n for n in nodes if n.get("role") == "cornerstone"]
    peripherals = [n for n in nodes if n.get("role") == "peripheral"]
    stats = {"papers": len(paper_ids), "edges": len(graph.get("edges", [])),
             "communities": len(communities), "years": [years[0], years[-1]] if years else [],
             "cornerstones": [n["title"] for n in cornerstones][:5],
             "peripherals_count": len(peripherals),
             "external_count": graph.get("external_count", 0)}
    hits, pack = _evidence_pack(paper_ids)
    titles = pack["titles"]

    community_note = "；".join(
        f"社区{c['id']}（{c['size']} 篇）" for c in communities[:6]) or "单社区"
    section_briefs = {
        "背景与问题": f"这 {len(paper_ids)} 篇论文共同的研究问题与动机。涉及论文：" +
                      "、".join((titles.get(p) or p)[:30] for p in paper_ids[:6]),
        "时间线与演进": f"按年份梳理方法演进脉络。年份跨度 {stats['years']}。",
        "方法族（社区）": f"按引用社区归纳方法流派：{community_note}。",
        "基石与边缘": f"基石论文（PageRank+高被引+早期）与边缘衍生论文的角色分化；"
                      f"基石示例：{'、'.join(stats['cornerstones']) or '（样本不足）'}。",
        "开放问题": "基于证据中的 limitation/future work 表述，提炼尚待解决的问题。",
    }

    sections: list[dict] = []
    guardrails_all: list[str] = []
    if not llm.available():
        skeleton = [f"## {name}\n\n{brief}\n\n（LLM 不可用：以上为图谱客观数据，综述正文待生成）"
                    for name, brief in section_briefs.items()]
        return {"sections": [{"title": n, "content": c, "claims": [], "anchors": {}}
                             for n, c in zip(_SURVEY_SECTIONS, skeleton)],
                "stats": stats, "guardrails": ["llm_unavailable_skeleton"],
                "content_md": "# 领域综述（骨架 · 图谱客观数据）\n\n" + "\n\n".join(skeleton)}

    for name in _SURVEY_SECTIONS:
        if progress:
            progress(name)
        system, pv = _resolve("insights", "system_survey",
                              "你是领域综述作者。只写证据支撑的事实，每论断带 [E#] 引用。")
        res = llm.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content":
                 f"综述小节：{name}\n小节要点：{section_briefs[name]}\n\n"
                 f"<evidence>\n{pack['context'][:6000]}\n</evidence>\n"
                 f"请写出该小节内容。"}],
            temperature=0.2, max_tokens=700, module="survey",
            audit_prompt=f"survey:{name}", prompt_version=pv)
        answer = (res.get("text") or "").strip()
        if not answer:
            answer = f"（{name} 生成失败，证据待补）"
        answer, g1 = validate_citations(answer, pack["eid_map"])
        claims, _ = llm_claims_safe(answer, hits)
        claims, g2 = verify_claims(claims, pack["eid_map"])
        guardrails_all += g1 + g2
        from ..rag.answering import build_anchors
        sections.append({"title": name, "content": answer, "claims": claims,
                         "anchors": build_anchors(pack["eid_map"])})

    content_md = ("# 领域综述（自动生成 · 机器草稿）\n\n"
                  + "\n\n".join(f"## {s['title']}\n\n{s['content']}" for s in sections))
    return {"sections": sections, "stats": stats, "guardrails": guardrails_all,
            "content_md": content_md}


# ---------- 模块五 P0-3：Future Work 外部最新进展核查 ----------

def _idea_sig(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (text or "").lower())).strip()[:120]


def _dedup_ideas(ideas: list[dict], threshold: float = 0.55) -> list[dict]:
    """语义去重（词元 Jaccard，离线确定性）：同一 idea 多篇提及合并，保最早论文为源。"""
    def toks(t: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]{3,}", t.lower())}

    kept: list[dict] = []
    kept_toks: list[set[str]] = []
    for idea in ideas:
        tk = toks(idea["idea_text"])
        dup = False
        for i, kt in enumerate(kept_toks):
            j = len(tk & kt) / max(len(tk | kt), 1)
            if j >= threshold:
                kept[i]["mention_papers"] = sorted(
                    set(kept[i].get("mention_papers", [kept[i]["source_paper"]]))
                    | {idea["source_paper"]})
                dup = True
                break
        if not dup:
            kept.append({**idea, "mention_papers": [idea["source_paper"]]})
            kept_toks.append(tk)
    return kept


def future_checks(paper_ids: list[str], *, max_ideas: int = 8, progress=None) -> list[dict]:
    """外部最新进展核查：抽取 → 语义去重 → S2 查后续论文 → 证据判定。

    诚实性约定：无外部证据时 verdict=open_unverified（与「库内确认没人做」的
    open 区分——外部没查到 ≠ 不存在）；solved/partial 必须点名来源论文+年份。"""
    ideas = _dedup_ideas(_extract_future_ideas(paper_ids))[:max_ideas]
    if progress:
        progress(f"extracted:{len(ideas)}")
    from ..rag.discovery import _s2_provider, title_norm

    out: list[dict] = []
    pending: list[int] = []
    for i, idea in enumerate(ideas):
        if progress:
            progress(f"external:{i + 1}/{len(ideas)}")
        ext: list[dict] = []
        try:
            r = _s2_provider(idea["idea_text"][:120], 5)
            if r.get("ok"):
                ext = [e for e in r.get("entries", [])
                       if (e.get("year") or 0) >= (idea.get("source_year") or 0)
                       and title_norm(e.get("title", "")) != ""][:4]
        except Exception as e:  # noqa: BLE001 —— 外部源失败 → open_unverified 如实
            logger.warning(f"future_check S2 失败: {e}", module="insights")
        item = {**idea, "verdict": "open_unverified", "confidence": 0.0,
                "external": [{"title": e["title"], "year": e.get("year", 0),
                              "citation_count": e.get("citation_count"),
                              "abstract": (e.get("abstract") or "")[:200]} for e in ext],
                "reason": ""}
        if ext:
            pending.append(i)
        else:
            item["reason"] = "未获取到外部进展证据（断网或无后续）→ 未验证，不冒充已排除"
        out.append(item)

    if pending and llm.available():
        try:
            system, pv = _resolve("insights", "system_future_check",
                                  "你是学术进展核查员。输出 JSON checks。")
            lines = []
            for i in pending:
                ev = "；".join(f"[{e['title'][:60]} ({e['year']})] {e['abstract'][:100]}"
                               for e in out[i]["external"]) or "（无）"
                lines.append(f'{{"id":{i},"idea":{json.dumps(out[i]["idea_text"], ensure_ascii=False)},'
                             f'"external":{json.dumps(ev, ensure_ascii=False)}}}')
            data = llm.chat_json_validated(
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"<ideas>\n[{', '.join(lines)}]\n</ideas>"}],
                FutureCheckList, retries=1, module="future_check",
                audit_prompt=out[pending[0]]["idea_text"][:200], prompt_version=pv,
                temperature=0.0)
            by_id = {int(c["id"]): c for c in (data or {}).get("checks", [])
                     if c.get("verdict") in ("solved", "partial", "open")}
            for i in pending:
                c = by_id.get(i)
                if c:
                    out[i]["verdict"] = c["verdict"]
                    out[i]["confidence"] = min(1.0, max(0.0, float(c.get("confidence", 0.5))))
                    out[i]["reason"] = c.get("reason", "")
        except Exception as e:  # noqa: BLE001 —— 判定失败 → 保持 open_unverified
            logger.warning(f"future_check 判定失败: {e}", module="insights")
    return out
