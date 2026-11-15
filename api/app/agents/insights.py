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
import math
import re
import statistics
from pathlib import Path

from .. import cache, db
from ..cache import TTLCache
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
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])|。\s*")

# 引用标记三种样式（真实语料实证：数字 [n]、分组数字 [13, 14]、字母键 [DGM06]/
# [RWC+19]、author-year (Devlin et al., 2019) / (Dolan and Brockett, 2005) /
# Devlin et al. (2019)）——统一在扫描时解析成 references 表 idx。
_CITE_BRACKET_RE = re.compile(r"\[([^\[\]\n]{1,40})\]")
_CITE_GROUP_NUM_RE = re.compile(r"^\d{1,3}(?:\s*[,;]\s*\d{1,3})*$")
_CITE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+._\-]{0,15}$")
_KEY_PREFIX_RE = re.compile(r"^\s*\[([A-Za-z][A-Za-z0-9+._\-]{0,15})\]\s*")
_AY_PAREN_RE = re.compile(r"\(([^()\n]{1,300})\)")
# (Surname et al., 2019) / (Surname & Other, 2018) / (Dolan and Brockett, 2005)
_AY_MULTI_RE = re.compile(
    r"^(?P<surname>[A-Z][A-Za-z'\u2019\-]+)\s+"
    r"(?:et\s+al\.?|and|&)\s*(?:(?:[A-Z][A-Za-z'\u2019\-]+)\s*)?\.?\s*,?\s*"
    r"(?P<year>(?:19|20)\d{2})(?P<suffix>[a-z]?)$")
# (Surname, 1953)
_AY_SINGLE_RE = re.compile(
    r"^(?P<surname>[A-Z][A-Za-z'\u2019\-]+)\s*,\s*"
    r"(?P<year>(?:19|20)\d{2})(?P<suffix>[a-z]?)$")
# 叙述式 Surname et al. (2019)
_AY_NARR_RE = re.compile(
    r"\b(?P<surname>[A-Z][A-Za-z'\u2019\-]+)\s+et\s+al\.?\s*\(\s*"
    r"(?P<year>(?:19|20)\d{2})(?P<suffix>[a-z]?)\s*\)")

# 含金量权重（答辩口径：实质性 1.0 / 背景 0.4 / 凑数 0.1）
_CLASS_WEIGHT = {"substantive": 1.0, "background": 0.4, "perfunctory": 0.1}

# LLM 增强分批参数：真实语料单篇引用句可达数千（UDL 991 条 refs），
# 一次塞 prompt 会截断/超时；分批 40 条、总量上限 240（成本可控，规则兜底）。
_CITE_LLM_BATCH = 40
_CITE_LLM_TOTAL_CAP = 240


def _load_docir(paper_id: str) -> dict:
    path = Path(DOCIR_DIR) / f"{paper_id}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— DocIR 损坏视同无文档
        return {}


def _year_of(paper: dict) -> int | None:
    """发表年份：arXiv YYMM → pub_year（1900..当前年+1 合理区间）→ created_at。

    真实语料实证：UDL 教材无 arXiv，原实现只看 created_at 变成 2026（下载入库年）；
    pub_year 由元数据层抽取（2023），必须优先于 created_at。"""
    m = _YEAR_RE.match(paper.get("arxiv_id") or "")
    if m:
        return 2000 + int(m.group(1))   # YY（前两位）为年份后两位，MM 为月份
    import datetime
    max_year = datetime.date.today().year + 1
    try:
        pub = int(paper.get("pub_year") or 0)
    except (TypeError, ValueError):
        pub = 0
    if 1900 <= pub <= max_year:
        return pub
    created = paper.get("created_at")
    if created:
        return datetime.datetime.fromtimestamp(created).year
    return None


# ---------- 图谱增强：社区 / 时间 / 角色 ----------

_S2_CIT_TTL = 7 * 86400.0   # 外部被引缓存 7 天（S2 共享池限速，必须缓存）


def _external_citation_counts(arxiv_ids: list[str]) -> dict[str, int]:
    """arXiv id → S2 外部被引数（S2 Graph batch API；meta_json 缓存 7 天）。

    用途：库内 20 篇的引用图统计意义稀疏（边只来自互相引用），基石判定需
    外部被引量做语义锚（BERT ~13 万 vs XLNet ~1 万，一档即换位）。
    失败/断网静默返回空 dict（角色判定回退纯库内信号，不阻塞）。"""
    import json as _json
    import time as _time

    import httpx as _httpx

    want = [a for a in dict.fromkeys(a.strip() for a in arxiv_ids if a and a.strip())]
    if not want:
        return {}
    out: dict[str, int] = {}
    need: list[str] = []
    rows = db.query("SELECT id, arxiv_id, meta_json FROM papers WHERE arxiv_id IN "
                    f"({','.join('?' * len(want))})", tuple(want))
    now = _time.time()
    for r in rows:
        try:
            meta = _json.loads(r["meta_json"] or "{}")
        except ValueError:
            continue
        cached = meta.get("s2_citations") or {}
        entry = cached.get("counts") or {}
        if cached.get("ts") and now - cached["ts"] < _S2_CIT_TTL:
            for aid, cnt in entry.items():
                out[aid] = int(cnt)
        else:
            need.append(r["arxiv_id"])
    fetch_ids = [a for a in want if a not in out and a not in need]
    need.extend(fetch_ids)
    if not need:
        return out
    try:
        resp = _httpx.post(
            "https://api.semanticscholar.org/graph/v1/paper/batch",
            params={"fields": "externalIds,citationCount"},
            json={"ids": [f"ARXIV:{a}" for a in need]},
            timeout=25,
        )
        resp.raise_for_status()
        by_arxiv = {}
        for item in resp.json() or []:
            if not item:
                continue
            aid = ((item.get("externalIds") or {}).get("ArXiv") or "").strip()
            if aid:
                by_arxiv[aid] = int(item.get("citationCount") or 0)
        out.update(by_arxiv)
        # 回写缓存：每个命中的论文行更新 meta_json.s2_citations
        if by_arxiv:
            for r in rows:
                aid = r["arxiv_id"]
                if aid in by_arxiv:
                    try:
                        meta = _json.loads(r["meta_json"] or "{}")
                    except ValueError:
                        meta = {}
                    meta["s2_citations"] = {"ts": now, "counts": {aid: by_arxiv[aid]}}
                    with db.tx() as c:
                        c.execute("UPDATE papers SET meta_json=? WHERE id=?",
                                  (_json.dumps(meta, ensure_ascii=False), r["id"]))
    except Exception as e:  # noqa: BLE001 —— S2 失败静默降级（纯库内信号兜底）
        logger.warning(f"S2 外部被引查询失败 → 纯库内信号: {type(e).__name__}: {e}",
                       module="insights")
    return out


def enrich(graph: dict) -> dict:
    """在 _citation_graph 产物上补 community/year/time_rank/role/modularity。"""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    papers = {r["id"]: dict(r) for r in db.query(
        "SELECT id,title,arxiv_id,created_at,pub_year FROM papers WHERE status='active'")}
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

    # 外部被引信号（Round6）：库内引用图边稀疏（仅互相引用），统计意义有限——
    # S2 外部被引量 ≥5000 的论文即便库内 PageRank 未达 P70 也具备基石的学术事实地位。
    arxiv_by_pid = {pid: (papers.get(pid, {}) or {}).get("arxiv_id") or ""
                    for pid in {n["id"] for n in nodes}}
    ext_cit = _external_citation_counts(list(arxiv_by_pid.values()))
    EXT_CIT_BASELINE = 5000

    out_nodes = []
    for m in metrics.get("nodes", []):
        pid = m["paper_id"]
        cid = communities.get(pid, 0)
        year = years.get(pid)
        role = ""
        role_reason = ""
        med = median_year.get(cid)
        ext_n = ext_cit.get(arxiv_by_pid.get(pid, ""), 0)
        if med is not None and year is not None:
            if year <= med and m["pagerank"] >= p70_pr and m["in_degree"] >= p70_in:
                role = "cornerstone"     # 基石论文：早期 + 高 PageRank + 高被引
                role_reason = (f"{year} 年发表（早于社区中位年 {med}）· "
                               f"PageRank {m['pagerank']:.3f} ≥ P70({p70_pr:.3f}) · "
                               f"被引 {m['in_degree']} ≥ P70({p70_in}) → 基石")
            elif (year <= med and ext_n >= EXT_CIT_BASELINE
                  and m["in_degree"] >= p70_in):
                role = "cornerstone"     # 外部证据通道：S2 被引量远超库内图的稀疏统计
                role_reason = (f"{year} 年发表（早于社区中位年 {med}）· "
                               f"库内 PageRank {m['pagerank']:.3f}（引用图边稀疏）· "
                               f"外部被引 {ext_n} ≥ {EXT_CIT_BASELINE}（S2）→ 基石")
            elif year > med and m["in_degree"] <= p30_in and cross_ratio.get(pid, 0) >= 0.5:
                role = "peripheral"      # 边缘衍生：新 + 低被引 + 跨社区桥接
                role_reason = (f"{year} 年发表（晚于社区中位年 {med}）· "
                               f"被引 {m['in_degree']} ≤ P30({p30_in}) · "
                               f"跨社区邻居占比 {cross_ratio.get(pid, 0):.0%} → 边缘衍生")
        fused = round(m["pagerank"] * math.log10(2 + ext_n), 4) if ext_n else m["pagerank"]
        out_nodes.append({**m, "year": year, "community": cid, "role": role,
                          "role_reason": role_reason,
                          "external_citations": ext_n,
                          "fused_score": fused,
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


# ---- 引用标记 → references 表 idx 的三种样式解析 ----

def _name_norm(name: str) -> str:
    """姓/名归一：只留字母数字并小写（Al-Rfou / Llu’is 等两侧一致比对）。"""
    return re.sub(r"[^0-9A-Za-z]", "", name or "").lower()


def _first_surname(authors: str, raw: str = "") -> str:
    """references.authors → 首作者姓。

    真实语料两种作者格式都要吃：given-first（"Alan Akbik, Duncan Blythe, and
    Roland Vollgraf" → Akbik）与 surname-first（"Devlin, Jacob, Chang, ..." →
    Devlin）。规则：去掉开头的 [KEY] 前缀 → 取第一个作者段（切 and / & / 逗号）→
    多于一个词取末词（given-first），单词本身即姓（surname-first）。"""
    a = re.sub(r"^\s*\[[^\]]+\]\s*", "", authors or "").strip()
    if not a and raw:
        a = re.sub(r"^\s*\[[^\]]+\]\s*", "", raw).strip()
    if not a:
        return ""
    first = re.split(r"\s+and\s+|&", a, maxsplit=1)[0]
    first = first.split(",", 1)[0].strip(" .;:")
    toks = [t for t in re.split(r"\s+", first) if t and t.lower() not in ("et", "al.", "al")]
    if not toks:
        return ""
    return _name_norm(toks[-1])


def _year_in_raw(raw: str) -> int:
    m = re.search(r"\b(19|20)\d{2}\b", raw or "")
    return int(m.group(0)) if m else 0


def _ref_row_fields(r) -> tuple[str, str, int]:
    """references 行 → (raw 文本, authors, year)。

    兼容旧库脏数据：真实库里 BART/UDL 两篇的 raw 列存的是整条 docir 参考条目
    JSON 字符串（authors/year/title 列为空）——解包后才能做 author-year 映射。
    正常行不受影响（仅当 authors 为空且 raw 以 "{" 开头才尝试解析）。"""
    raw = r["raw"] or ""
    authors = r["authors"] or ""
    try:
        year = int(r["year"] or 0)
    except (TypeError, ValueError):
        year = 0
    stripped = raw.lstrip()
    if not authors and stripped.startswith("{"):
        try:
            blob = json.loads(raw)
        except ValueError:
            blob = None
        if isinstance(blob, dict):
            raw = str(blob.get("raw") or raw)
            authors = str(blob.get("authors") or "")
            try:
                year = int(blob.get("year") or year)
            except (TypeError, ValueError):
                pass
    return raw, authors, year or _year_in_raw(raw)


def _ref_context(rows: list) -> dict:
    """单篇 references 行 → 三种样式的查找表。

    - keys: raw 开头的 `[KEY]` → idx（字母键样式，如 GPT-3 的 [DGM06]/[RWC+19]）
    - ay:   (首作者姓归一, year) → idx（同姓同年取 idx 最小的一条，歧义不硬猜其他）
    - ay_suffix: (姓, year, 年份字母后缀 a/b) → idx（Peters et al. 2018a/2018b 消歧）
    """
    ctx: dict = {"by_idx": {}, "keys": {}, "ay": {}, "ay_suffix": {}}
    for r in sorted(rows, key=lambda x: int(x["idx"] or 0)):
        idx = int(r["idx"] or 0)
        if idx <= 0:
            continue
        raw, authors, year = _ref_row_fields(r)
        ctx["by_idx"][idx] = r
        km = _KEY_PREFIX_RE.match(raw)
        if km:
            ctx["keys"].setdefault(km.group(1), idx)
        surname = _first_surname(authors, raw)
        if surname and year:
            ctx["ay"].setdefault((surname, year), idx)
            suf = re.search(rf"\b{year}([a-z])\b", raw)
            if suf:
                ctx["ay_suffix"].setdefault((surname, year, suf.group(1)), idx)
    return ctx


def _lookup_ay(surname: str, year: int, suffix: str, ctx: dict) -> int | None:
    key = _name_norm(surname)
    if not key or not year:
        return None
    if suffix:
        hit = ctx["ay_suffix"].get((key, year, suffix))
        if hit:
            return hit
    return ctx["ay"].get((key, year))


def _resolve_ay_chunk(chunk: str, ctx: dict) -> int | None:
    """括号内一段 author-year 文本 → ref idx（未命中返回 None，保持中性不硬猜）。"""
    s = chunk.strip()
    s = re.sub(r"^(?:e\.g\.,?|see also|see|cf\.)\s+", "", s, flags=re.IGNORECASE)
    s = re.sub(r",?\s*(?:p{1,2}\.|pp\.)\s*\d+\s*$", "", s)
    m = _AY_MULTI_RE.match(s) or _AY_SINGLE_RE.match(s)
    if not m:
        return None
    return _lookup_ay(m.group("surname"), int(m.group("year")),
                      m.group("suffix") or "", ctx)


def _scan_citation_refs(text: str, ctx: dict) -> list[int]:
    """一句文本 → 命中 ref idx 列表（数字/分组数字/字母键/author-year 四通道）。"""
    refs: list[int] = []
    for m in _CITE_BRACKET_RE.finditer(text):
        content = m.group(1).strip()
        if _CITE_GROUP_NUM_RE.match(content):
            for part in re.split(r"[,;]", content):
                part = part.strip()
                if part.isdigit():
                    refs.append(int(part))
        elif _CITE_KEY_RE.match(content):
            idx = ctx["keys"].get(content)
            if idx is None:
                idx = ctx["keys"].get(content.upper())
            if idx is not None:
                refs.append(idx)
    for m in _AY_PAREN_RE.finditer(text):
        for chunk in m.group(1).split(";"):
            if not chunk.strip():
                continue
            idx = _resolve_ay_chunk(chunk, ctx)
            if idx is not None:
                refs.append(idx)
    for m in _AY_NARR_RE.finditer(text):
        idx = _lookup_ay(m.group("surname"), int(m.group("year")),
                         m.group("suffix") or "", ctx)
        if idx is not None:
            refs.append(idx)
    return refs


# 引用含金量缓存（LLM 分类昂贵）：键 = sorted(paper_ids)+data_revision，10min TTL。
# 上传/解析完成 bump revision → 批次内容变化自然失效。
_cite_cls_cache: TTLCache | None = None


def _cite_cache() -> TTLCache:
    global _cite_cls_cache
    if _cite_cls_cache is None:
        _cite_cls_cache = TTLCache(maxsize=16, ttl=600.0)
    return _cite_cls_cache


def classify_citations(paper_ids: list[str], graph: dict | None = None) -> dict:
    """扫描正文引用标记 → 句子级分类 → 边含金量加权。

    支持四种真实语料样式并统一解析成 references 表 idx：数字 [n]、分组数字
    [13, 14]、字母键 [DGM06]/[RWC+19]、author-year (Devlin et al., 2019) /
    (Dolan and Brockett, 2005) / Devlin et al. (2019)。mention key 仍为
    "pid|idx"，下游 ref_target/edge_weights 逻辑不变。

    产出：edge_weights{"src|dst": Σw}、class_stat 计数、mention_count、guardrails。
    LLM 可用时分批增强（每批 40、总量上限 240；任一批失败该批规则兜底并记
    guardrail，不影响整体）。graph 可传入外部已建引用图；缺省自建。
    结果按 (sorted ids, data_revision) 缓存 10min——重复导航不再触发 LLM 分类。
    """
    ids_key = tuple(sorted(paper_ids))
    cached = _cite_cache().get(cache.rev_key("cite_cls", ids_key))
    if cached is not None:
        return cached[0]
    graph = graph if graph is not None else _citation_graph(paper_ids)
    # ref idx → 库内目标（与 _citation_graph 相同的匹配规则）
    papers = {r["id"]: dict(r) for r in db.query("SELECT id,title,arxiv_id FROM papers WHERE status='active'")}
    by_arxiv = {p["arxiv_id"]: pid for pid, p in papers.items() if p.get("arxiv_id")}
    by_title = {p["title"].strip().lower(): pid for pid, p in papers.items() if p.get("title")}

    mentions: list[dict] = []       # [{key:"pid|idx", sentence, section}]
    refs_by_pid: dict[str, list] = {}
    for pid in paper_ids:
        docir = _load_docir(pid)
        rows = db.query('SELECT idx,title,arxiv_id,raw,authors,year FROM "references" '
                        "WHERE paper_id=?", (pid,))
        refs_by_pid[pid] = rows
        ctx = _ref_context(rows)
        for b in docir.get("blocks", []):
            if b.get("type") not in ("paragraph", "heading"):
                continue
            text = b.get("text") or ""
            # 快速预筛：无方括号/圆括号/叙述式作者 → 必无引用标记
            if "[" not in text and "(" not in text and "et al" not in text.lower():
                continue
            section = b.get("section_path") or ""
            for sent in _SENT_SPLIT_RE.split(text):
                sent = sent.strip()
                if not sent:
                    continue
                for idx in _scan_citation_refs(sent, ctx):
                    mentions.append({"key": f"{pid}|{idx}",
                                     "sentence": sent[:200],
                                     "section": section})

    # LLM 分批增强（可选；编号即 mentions 下标；每批失败只影响该批→规则兜底）
    cls_by_idx: dict[int, str] = {}
    guardrails: list[str] = []
    if mentions and llm.available() and settings.semantic_judge:
        try:
            system, pv = _resolve("insights", "system_classify",
                                  "你是引用含金量分析器。输出 JSON 分类。")
        except Exception as e:  # noqa: BLE001 —— prompt 缺失不阻塞规则分类
            system = pv = None
            guardrails.append("cite_llm_prompt_unavailable")
            logger.warning(f"含金量 LLM prompt 解析失败 → 全量规则兜底: {e}", module="insights")
        if system:
            cap = min(len(mentions), _CITE_LLM_TOTAL_CAP)
            for start in range(0, cap, _CITE_LLM_BATCH):
                batch = list(range(start, min(start + _CITE_LLM_BATCH, cap)))
                try:
                    lines = [
                        f'{{"id":{i},"sentence":{json.dumps(mentions[i]["sentence"], ensure_ascii=False)},'
                        f'"section":{json.dumps(mentions[i]["section"], ensure_ascii=False)}}}'
                        for i in batch]
                    data = llm.chat_json_validated(
                        [{"role": "system", "content": system},
                         {"role": "user", "content": f"<mentions>\n[{', '.join(lines)}]\n</mentions>"}],
                        CitationClassList, retries=1, module="cite_classify",
                        audit_prompt=mentions[start]["sentence"][:200], prompt_version=pv,
                        temperature=0.0)
                    for c in (data or {}).get("classes", []):
                        try:
                            ci = int(c.get("idx"))
                        except (TypeError, ValueError):
                            continue
                        if ci in batch and c.get("cls") in _CLASS_WEIGHT:
                            cls_by_idx[ci] = c["cls"]
                except Exception as e:  # noqa: BLE001 —— 该批失败规则兜底，不影响整体
                    guardrails.append(
                        f"cite_llm_batch_{start // _CITE_LLM_BATCH + 1}_rule_fallback")
                    logger.warning(
                        f"含金量 LLM 增强批次 {start // _CITE_LLM_BATCH + 1} 失败 → 该批规则兜底: {e}",
                        module="insights")

    # ref idx → target
    ref_target: dict[str, str | None] = {}
    for pid in paper_ids:
        for ref in refs_by_pid.get(pid, []):
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

    result = {"edges": weighted_edges, "edge_weights": edge_weights,
              "class_stat": class_stat, "mention_count": len(mentions),
              "guardrails": guardrails, "enriched": enriched}
    _cite_cache().set(cache.rev_key("cite_cls", ids_key), result)
    return result


def reference_quality(paper_id: str) -> dict:
    """单篇参考文献含金量（模块四 P0-2）：ref idx → {cls, weight, mentions, examples}。

    供文献下钻/References 面板展示与可逆过滤：无引用句的条目不出现（前端显式
    「无引用句」中性态，不假装判断）。cls 取该条所有引用句中权重最高者的分类。
    另返回 mention_count（全文命中引用句条数）、coverage（有引用句条目/总 refs）
    与 class_stat/guardrails——覆盖多少如实标注，不把「没扫到」冒充「没有引用」。"""
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
    ref_rows = db.query('SELECT idx FROM "references" WHERE paper_id=?', (paper_id,))
    total_refs = len(ref_rows)
    ref_idxs: set[int] = set()
    for ref in ref_rows:
        try:
            ref_idxs.add(int(ref["idx"]))
        except (TypeError, ValueError):
            continue
    covered = len(set(out) & ref_idxs)     # 只算真实存在的 ref idx，coverage 恒 ≤1
    return {"paper_id": paper_id, "qualities": out,
            "class_stat": r.get("class_stat", {}),
            "mention_count": int(r.get("mention_count", 0)),
            "coverage": round(covered / total_refs, 3) if total_refs else 0.0,
            "covered_refs": covered,
            "total_refs": total_refs,
            "guardrails": r.get("guardrails", []),
            "note": "无引用句的条目不在 qualities 中（中性态，不做判断）；"
                    "coverage = 有引用句条目 / 总参考文献数",
            }


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

# future 关键句：只认明确的 future 意图短语。裸 "further"/"remains" 会把
# "Our major contribution is further generalizing…"/"remains competitive" 这类
# 贡献总结句当 future work（真实语料实证噪声），因此收紧为短语级匹配。
_FUTURE_KEYWORDS = ("future work", "future works", "future direction", "future research",
                    "future study", "future studies", "future investigation",
                    "future exploration", "future extension", "future applications",
                    "in the future", "we plan", "we intend", "we leave",
                    "further work", "further research", "further study",
                    "further investigation", "further explore", "further exploration",
                    "remains to be", "remains open", "remains unclear",
                    "remains challenging", "remains unexplored", "remains an open")
_FUTURE_ZH = ("未来工作", "后续研究", "进一步", "有待")

_TAIL_SECTIONS = ("conclusion", "limitation", "discussion", "future", "concluding")
_TAIL_SECTIONS_ZH = ("结论", "局限", "讨论", "展望", "总结")
_TAIL_HEAD_RE = re.compile(r"(conclusion|concluding|limitation|discussion|future|"
                           r"结论|局限|讨论|展望|总结)", re.IGNORECASE)
# 后区边界：References/Appendix/Acknowledgements/Index 之后不再是论文尾章
_BACKMATTER_HEAD_RE = re.compile(
    r"\b(references|bibliography|appendix|appendices|acknowledg\w*|index|notation|glossary)\b",
    re.IGNORECASE)
# 附录编号标题（"A.5 Discussion and Analysis"）：不能当论文尾章用
_APPENDIX_LABEL_RE = re.compile(r"^\s*[A-Z](?:\s*\.\s*\d+)*\.?\s+[A-Z]")
# 非 actionable 句（真实语料噪声实证）：附录指路/更形式化分析/纯引用行
_NON_ACTIONABLE_RE = re.compile(
    r"please\s+refer|refer\s+(?:the\s+reader\s+)?to\s+(?:the\s+)?"
    r"(?:appendix|appendices|supplementary|supp\.)"
    r"|for\s+(?:more|further)\s+(?:formal\s+|detailed\s+|comprehensive\s+|thorough\s+)?"
    r"(?:analysis|details?|discussion|information|results?)"
    r"|see\s+(?:the\s+)?(?:appendix|appendices|supplementary)"
    r"|(?:in|given\s+in|shown\s+in|presented\s+in|described\s+in)\s+(?:the\s+)?"
    r"(?:appendix|appendices)"
    r"|appendix\s+[A-Z0-9]"
    r"|we\s+(?:discuss|present|detail|describe|summari[sz]e)\s+(?:the\s+|our\s+)?"
    r"(?:limitations?|future\s+(?:work|directions?))"
    r"|we\s+hope\s+(?:our|this)\s+(?:work|paper)\s+inspires?"
    r"|^(?:there\s+are\s+)?(?:many|several)\s+(?:directions|avenues)\s+for\s+future"
    r"|详细(?:内容|推导|分析|结果)?(?:请)?见|见附录|参见附录|附录",
    re.IGNORECASE)


def _plain_block_text(text: str) -> str:
    """PDF 文本清洗：连字符跨行合词、空白归一。"""
    t = re.sub(r"-\s*\n\s*", "", text or "")
    return re.sub(r"\s+", " ", t).strip()


def _split_sentences(text: str) -> list[str]:
    t = _plain_block_text(text)
    if not t:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(\"“‘])|(?<=[。！？])", t)
    return [p.strip() for p in parts if p.strip()]


def _section_has_tail(section: str) -> bool:
    s = (section or "").lower()
    return any(k in s for k in _TAIL_SECTIONS) or any(k in section for k in _TAIL_SECTIONS_ZH)


def _tail_blocks(docir: dict) -> list[dict]:
    """按 heading 块序列定位尾章（Conclusion/Limitation/Discussion/Future/Concluding）
    后续段落；无 heading（旧解析）才退到 section_path 尾章匹配。

    旧实现兜底取「末尾 6 块」——真实语料里那 6 块常是 References/Appendix 表格
    残片，是 "For more formal analysis…" 类噪声的根因，已移除。"""
    blocks = docir.get("blocks") or []
    if not blocks:
        return []
    heading_idx = [i for i, b in enumerate(blocks) if b.get("type") == "heading"]
    if not heading_idx:
        out = [b for b in blocks if b.get("type") == "paragraph"
               and _section_has_tail(b.get("section_path") or "")]
        return out[:8]
    boundary = len(blocks)
    for i in heading_idx:
        if _BACKMATTER_HEAD_RE.search(blocks[i].get("text") or ""):
            boundary = i
            break
    tail_starts = [
        i for i in heading_idx
        if i < boundary
        and _TAIL_HEAD_RE.search(blocks[i].get("text") or "")
        and not _APPENDIX_LABEL_RE.match((blocks[i].get("text") or "").strip())]
    if not tail_starts:
        return []
    out: list[dict] = []
    for b in blocks[tail_starts[-1] + 1:]:
        if b.get("type") == "heading":
            break               # 只取该尾章标题后的连续段落
        if b.get("type") == "paragraph" and (b.get("text") or "").strip():
            out.append(b)
        if len(out) >= 8:
            break
    return out


def _is_actionable_future_sentence(sent: str) -> bool:
    """尾章句子 → 是否可执行的 future/limitation 句（过滤附录指路与引用行）。"""
    s = (sent or "").strip()
    if len(s) < 25:
        return False
    low = s.lower()
    if _NON_ACTIONABLE_RE.search(s):
        return False
    # 参考文献条目：以 [n]/[KEY] 开头
    if re.match(r"^\s*(?:\[\d{1,3}\]|\[[A-Za-z][A-Za-z0-9+._\-]{0,15}\])", s):
        return False
    # 纯引用/书目行特征
    if re.search(r"(arxiv preprint|in proceedings|in advances in|pp\.\s*\d+\s*[-–]\s*\d+)",
                 low):
        return False
    # 表格/公式残片：可读词太少
    if len(re.sub(r"[\W\d_]", "", s)) < 12:
        return False
    return any(k in low for k in _FUTURE_KEYWORDS) or any(k in s for k in _FUTURE_ZH)


def _extract_future_ideas(paper_ids: list[str]) -> list[dict]:
    """尾章抽取：规则（future 关键句）→ LLM 批量概括（失败逐句直用）。

    每条 idea 带 source_year（来自 _year_of：arXiv YYMM → pub_year → created_at），
    供 future_checks 的「只保留 year >= source_year」外部证据过滤。"""
    papers: dict[str, dict] = {}
    wanted = list(dict.fromkeys(paper_ids))
    if wanted:
        rows = db.query("SELECT id,arxiv_id,pub_year,created_at FROM papers WHERE id IN ("
                        f"{','.join('?' * len(wanted))})", tuple(wanted))
        papers = {r["id"]: dict(r) for r in rows}
    ideas: list[dict] = []
    for pid in paper_ids:
        docir = _load_docir(pid)
        source_year = _year_of(papers.get(pid, {}))
        cand: list[str] = []
        seen_sent: set[str] = set()
        for b in _tail_blocks(docir):
            for sent in _split_sentences(b.get("text") or ""):
                if not _is_actionable_future_sentence(sent):
                    continue
                sig = re.sub(r"\s+", " ", sent.lower())[:160]
                if sig in seen_sent:
                    continue
                seen_sent.add(sig)
                cand.append(sent[:300])
        if not cand:
            continue
        if llm.available():
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
                                  "source_year": source_year,
                                  "anchor": _anchor_of(docir, cand[0])} for t in got[:5])
                    continue
            except Exception as e:  # noqa: BLE001 —— LLM 概括失败 → 原句直用
                logger.warning(f"future 概括失败 → 原句直用: {e}", module="insights")
        ideas.extend({"idea_text": c, "source_paper": pid, "source_year": source_year,
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


# ---- idea 词元：英文词 + CJK bigram ----
# 旧 tokenizer 只认 [a-z]{3,}：中文 idea 词元集恒空 → Jaccard 恒 0 → 中文去重永不生效。
_EN_TOKEN_RE = re.compile(r"[a-z]{3,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_EN_STOP = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "has",
    "have", "into", "their", "our", "its", "can", "could", "should", "would", "may",
    "might", "will", "using", "use", "used", "such", "than", "then", "when", "where",
    "which", "while", "also", "but", "not", "via", "towards", "toward", "over", "under",
    "between", "about", "more", "most", "much", "many", "some", "any", "all", "each",
    "both", "other", "these", "those", "there", "here", "what", "how", "why", "who",
    "does", "did", "being", "been", "is", "of", "to", "in", "on", "by", "as", "at",
    "or", "an", "a", "we", "it", "be",
})


def _idea_tokens(text: str) -> set[str]:
    """英文词 + CJK bigram（连续汉字两两切分；单字段保留单字）词元集。"""
    t = (text or "").lower()
    toks = set(_EN_TOKEN_RE.findall(t))
    for run in _CJK_RUN_RE.findall(t):
        if len(run) == 1:
            toks.add(run)
        else:
            toks.update(run[i:i + 2] for i in range(len(run) - 1))
    return toks


def _rule_tokens(text: str) -> set[str]:
    return {w for w in _idea_tokens(text) if w not in _EN_STOP}


def _dedup_ideas(ideas: list[dict], threshold: float = 0.55) -> list[dict]:
    """语义去重（词元 Jaccard，离线确定性）：同一 idea 多篇提及合并，保最早论文为源。

    空词元集不参与匹配（避免 "" 与任何文本 Jaccard=0 或除零误判）；
    合并时 source_year 取较早者，mention_papers 追加。"""
    kept: list[dict] = []
    kept_toks: list[set[str]] = []
    for idea in ideas:
        tk = _idea_tokens(idea["idea_text"])
        dup_idx = -1
        if tk:
            for i, kt in enumerate(kept_toks):
                if not kt:
                    continue
                j = len(tk & kt) / max(len(tk | kt), 1)
                if j >= threshold:
                    dup_idx = i
                    break
        if dup_idx >= 0:
            kept[dup_idx]["mention_papers"] = sorted(
                set(kept[dup_idx].get("mention_papers", [kept[dup_idx]["source_paper"]]))
                | {idea["source_paper"]})
            sy = idea.get("source_year")
            cur = kept[dup_idx].get("source_year")
            if sy is not None and (cur is None or sy < cur):
                kept[dup_idx]["source_year"] = sy
        else:
            kept.append({**idea, "mention_papers": [idea["source_paper"]]})
            kept_toks.append(tk)
    return kept


# ---- 外部证据链：S2 → OpenAlex → arXiv（单源失败/无可用结果静默下一个） ----

def _ext_year(entry: dict) -> int:
    raw = entry.get("year")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        m = re.search(r"\b(19|20)\d{2}\b", str(raw or ""))
        return int(m.group(0)) if m else 0


_RULE_TOKEN_OVERLAP = 0.5


def _rule_overlap(idea_text: str, external: list[dict]) -> float:
    """idea 词元被外部 title+abstract 覆盖的比例（去停用词，0~1）。"""
    it = _rule_tokens(idea_text)
    if not it:
        return 0.0
    best = 0.0
    for e in external:
        et = _rule_tokens(f"{e.get('title') or ''} {e.get('abstract') or ''}")
        if not et:
            continue
        best = max(best, len(it & et) / len(it))
    return best


def _rule_verdict(idea_text: str, external: list[dict]) -> tuple[str, float, str] | None:
    """LLM 不可用/失败时的规则初判：词元重叠 ≥0.5 → partial（置信度 0.4，明标规则）。"""
    overlap = _rule_overlap(idea_text, external)
    if overlap >= _RULE_TOKEN_OVERLAP:
        msg = (f"规则初判：idea 与外部证据 title+abstract 词元重叠 {overlap:.2f} ≥ "
               f"{_RULE_TOKEN_OVERLAP} → partial（LLM 未参与，置信度 0.4）")
        return ("partial", 0.4, msg)
    return None


def future_checks(paper_ids: list[str], *, max_ideas: int = 8, progress=None) -> list[dict]:
    """外部最新进展核查：抽取 → 语义去重 → 多源查后续论文 → 证据判定。

    外部证据链 S2 → OpenAlex → arXiv（单源失败或无 year≥source_year 的可用条目
    静默下一个），按 title_norm 去重，证据记录 source。
    诚实性约定：无外部证据时 verdict=open_unverified（与「库内确认没人做」的
    open 区分——外部没查到 ≠ 不存在）；LLM 不可用时做规则初判（词元重叠 ≥0.5 →
    partial/0.4，verdict_source=rule，evidence 里明标规则来源），否则不冒充。"""
    ideas = _dedup_ideas(_extract_future_ideas(paper_ids))[:max_ideas]
    if progress:
        progress(f"extracted:{len(ideas)}")
    from ..rag import discovery as _disc

    out: list[dict] = []
    pending: list[int] = []
    for i, idea in enumerate(ideas):
        if progress:
            progress(f"external:{i + 1}/{len(ideas)}")
        sy = idea.get("source_year")
        ext: list[dict] = []
        seen_titles: set[str] = set()
        for name in ("s2", "openalex", "arxiv"):
            provider = getattr(_disc, f"_{name}_provider", None)
            if provider is None:
                continue
            try:
                r = provider(idea["idea_text"][:120], 5)
            except Exception as e:  # noqa: BLE001 —— 单源失败静默下一个
                logger.warning(f"future_check {name} 失败 → 下一源: {e}", module="insights")
                continue
            if not r or not r.get("ok"):
                continue
            got: list[dict] = []
            for e in r.get("entries") or []:
                tn = _disc.title_norm(e.get("title", "") or "")
                if not tn or tn in seen_titles:
                    continue
                y = _ext_year(e)
                if sy is not None:
                    if y < sy:               # 只保留不早于 idea 来源论文的进展
                        continue
                elif y <= 0:
                    continue
                seen_titles.add(tn)
                got.append({"title": e.get("title") or "", "year": y,
                            "citation_count": e.get("citation_count"),
                            "abstract": (e.get("abstract") or "")[:200],
                            "source": name})
            if got:
                ext.extend(got)
                break                        # 该源已有可用证据，不再叠加（避免重复/时延）
        ext = ext[:4]
        item = {**idea, "verdict": "open_unverified", "confidence": 0.0,
                "verdict_source": "", "external": ext, "reason": ""}
        if ext:
            pending.append(i)
        else:
            item["reason"] = ("未获取到外部进展证据（S2/OpenAlex/arXiv 均无命中或断网）"
                              "→ 未验证，不冒充已排除")
        out.append(item)

    # LLM 判定（有外部证据的）
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
                    out[i]["verdict_source"] = "llm"
        except Exception as e:  # noqa: BLE001 —— 判定失败 → 规则初判兜底
            logger.warning(f"future_check 判定失败 → 规则初判兜底: {e}", module="insights")

    # 规则初判兜底：LLM 不可用/未给出结论时，只对有外部证据的条目，明标规则来源
    for i in pending:
        if out[i]["verdict"] != "open_unverified":
            continue
        rv = _rule_verdict(out[i]["idea_text"], out[i]["external"])
        if rv is None:
            out[i]["reason"] = ("有外部候选论文，但 LLM 未参与且 idea 与证据词元重叠 < "
                                f"{_RULE_TOKEN_OVERLAP} → 未验证，不冒充已排除")
            continue
        verdict, conf, reason = rv
        out[i]["verdict"] = verdict
        out[i]["confidence"] = conf
        out[i]["reason"] = reason
        out[i]["verdict_source"] = "rule"
        # verdict_source 落 evidence JSON（graph_routes 落库 evidence=external[]）
        for ev in out[i]["external"]:
            ev["verdict_source"] = "rule"
    return out
