"""结构化元数据抽取（模块一 P0：标题/作者/机构/摘要/venue/doi/year）。

分层降级（上层失败自动落下一层，全失败时保留解析引擎的启发式结果，闭环不中断）：
1. arXiv API：已知 arXiv 基 id 时查 export.arxiv.org Atom feed——作者/摘要/年份/分类/
   journal_ref 最准；结果缓存进 papers.meta_json（重解析零重复请求）。
2. LLM：首页文本喂 chat_json_validated（authors[{name,affiliation}]/abstract/venue/year/doi），
   覆盖扫描件与非 arXiv 论文；arXiv Atom 不含作者机构 → 此层专职补 affiliation。
3. 启发式：Abstract/摘要 标题后继块抽摘要（兜底，离线可用）。

meta_source 如实标注来源（arxiv_api / llm / heuristic / arxiv_api+llm），
缺失字段保持为空——诚实降级，不伪造。
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

import httpx
from pydantic import BaseModel, Field

from .. import db
from ..llm import client
from ..log import logger

_ATOM = "http://www.w3.org/2005/Atom"
_ARXIV = "http://arxiv.org/schemas/atom"


class AuthorMeta(BaseModel):
    name: str = ""
    affiliation: str = ""


class MetaSchema(BaseModel):
    """LLM 抽取目标 schema（首页信息足够回答：作者是谁、属哪个机构、摘要是什么）。"""

    title: str = ""
    authors: list[AuthorMeta] = Field(default_factory=list)
    abstract: str = ""
    venue: str = ""
    year: int = 0
    doi: str = ""


# ---------- 第 1 层：arXiv API ----------

def fetch_arxiv_meta(arxiv_base: str, timeout: float = 8.0) -> dict | None:
    """arXiv 基 id → {title, authors:[{name,affiliation}], abstract, year, categories, doi}。

    affiliation arXiv Atom 不提供 → 返回 affiliation 留空，由 LLM 层补齐。
    title 供 stem 形标题回填与 Crossref 题名检索。
    失败返回 None（网络/限流/无效 id 均降级，绝不抛出阻断解析）。
    """
    base = (arxiv_base or "").strip()
    if not re.match(r"^\d{4}\.\d{4,5}$", base):
        return None
    try:
        resp = httpx.get(
            "https://export.arxiv.org/api/query",
            params={"id_list": base, "max_results": 1},
            timeout=timeout,
            headers={"User-Agent": "evitrace/1.0 (academic-assistant)"},
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as e:  # noqa: BLE001 —— 网络故障降级到下一层
        logger.warning(f"arXiv API 查询失败 id={base}: {type(e).__name__}: {e}", module="meta")
        return None
    entry = root.find(f"{{{ _ATOM }}}entry")
    if entry is None:
        return None
    title = re.sub(r"\s+", " ", (entry.findtext(f"{{{ _ATOM }}}title") or "")).strip()
    authors: list[dict] = []
    for a in entry.findall(f"{{{ _ATOM }}}author"):
        name = (a.findtext(f"{{{ _ATOM }}}name") or "").strip()
        aff = (a.findtext(f"{{{ _ARXIV }}}affiliation") or "").strip()  # 新版 feed 偶有
        if name:
            authors.append({"name": name, "affiliation": aff})
    summary = re.sub(r"\s+", " ", (entry.findtext(f"{{{ _ATOM }}}summary") or "")).strip()
    published = entry.findtext(f"{{{ _ATOM }}}published") or ""
    year = int(published[:4]) if published[:4].isdigit() else 0
    doi = (entry.findtext(f"{{{ _ARXIV }}}doi") or "").strip()
    journal_ref = (entry.findtext(f"{{{ _ARXIV }}}journal_ref") or "").strip()
    categories = [c.get("term", "") for c in entry.findall(f"{{{ _ATOM }}}category")]
    return {"title": title, "authors": authors, "abstract": summary, "year": year,
            "doi": doi, "journal_ref": journal_ref,
            "categories": [c for c in categories if c]}


# ---------- 第 2 层：LLM 首页抽取 ----------

_LLM_PROMPT = (
    "从下面这篇论文的第一页文本中抽取结构化元数据。只输出 JSON，字段：\n"
    "title: 论文标题（逐字，不要改写）\n"
    "authors: [{name, affiliation}]（作者与其机构，机构看标题下的上标对应；推不出机构则留空串）\n"
    "abstract: 摘要原文（不要改写）\nvenue: 发表载体（会议/期刊名，推不出留空）\n"
    "year: 发表年份（整数，推不出 0）\ndoi: DOI（推不出留空）\n"
    "注意：只依据给定文本，缺失字段留空/0，不得编造。\n\n论文第一页文本：\n"
)

_META_CACHE_PREFIX = "arxiv_meta_cache:"
_CROSSREF_CACHE_PREFIX = "crossref_cache:"
_DOI_RE = re.compile(r"10\.\d{4,9}/[\w.\-/:]+", re.IGNORECASE)
_STEM_TITLE = re.compile(r"^(p_[0-9a-f]{6,}|seg_\d{2,}|fx_\w+)$", re.IGNORECASE)


def llm_extract(first_page_text: str) -> dict | None:
    """首页文本 → MetaSchema dict；LLM 不可用/解析失败返回 None。"""
    text = (first_page_text or "").strip()[:3000]
    if len(text) < 50:
        return None
    res = client.chat_json_validated(
        [{"role": "user", "content": _LLM_PROMPT + text}], MetaSchema,
        module="meta_extract", audit_prompt="[meta_extract] first page",
        prompt_version="meta-v2")
    if not res:
        return None
    try:
        data = MetaSchema(**res).model_dump()
    except Exception:  # noqa: BLE001 —— 校验失败降级
        return None
    data["authors"] = [{"name": a["name"].strip(), "affiliation": a.get("affiliation", "").strip()}
                       for a in data["authors"] if a.get("name", "").strip()]
    return data


# ---------- 第 3 层：启发式摘要 ----------

def heuristic_abstract(docir: dict) -> str:
    """Abstract/摘要 标题（或行内形式）之后的连续块拼摘要；找不到返回 ''。

    细粒度 P1/P2：支持 IEEE 行内形式 "Abstract—We propose…"（em/en-dash 无冒号）；
    书籍前言（前言/序言/序/Preface/Foreword）取首段 ≤600 字（meta 语义为文档自述首段）。
    """
    blocks = docir.get("blocks", [])
    for i, b in enumerate(blocks):
        t = (b.get("text") or "").strip()
        if not t or b.get("page", 0) > 5:
            break
        if re.match(r"^(abstract|摘要)\b[:：]?\s*$", t, re.IGNORECASE):
            parts: list[str] = []
            for nb in blocks[i + 1:]:
                if nb.get("page", 0) > b.get("page", 0) + 1:
                    break
                nt = (nb.get("text") or "").strip()
                if re.match(r"^(keywords|关键词|index terms|1[\s.．]|introduction\b)", nt, re.IGNORECASE):
                    break
                if nt:
                    parts.append(nt)
                if sum(len(p) for p in parts) > 2500:
                    break
            return " ".join(parts)
        m = re.match(r"^(?:abstract|摘要)\b[:：—–-]\s*(.+)$", t, re.IGNORECASE)
        if m and len(m.group(1)) > 80:
            return m.group(1)
        # 书籍前言形态：标题命中后取首段（前言是文档自述，非严格"摘要"，仅离线兜底）
        if re.match(r"^(前言|序言|序|preface|foreword)\b[:：]?\s*$", t, re.IGNORECASE):
            parts = []
            for nb in blocks[i + 1:]:
                if nb.get("page", 0) > b.get("page", 0) + 2:
                    break
                nt = (nb.get("text") or "").strip()
                if re.match(r"^(keywords|关键词|\d[\s.．]|chapter\b|第\s*[一二三1-9一-龥]+\s*[章讲])", nt, re.IGNORECASE):
                    break
                if nt:
                    parts.append(nt)
                if sum(len(p) for p in parts) > 600:
                    break
            if parts:
                return " ".join(parts)[:600]
    return ""


def first_page_text(docir: dict, max_chars: int = 3000) -> str:
    parts: list[str] = []
    total = 0
    for b in docir.get("blocks", []):
        if b.get("page", 0) > 1:
            break
        t = (b.get("text") or "").strip()
        if t and b.get("type") != "figure":
            parts.append(t)
            total += len(t)
            if total >= max_chars:
                break
    return "\n".join(parts)[:max_chars]


# ---------- Crossref 兜底（细粒度 P0-3，用户选定外呼方案） ----------

def _norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip().casefold()


def _title_stem_like(meta: dict) -> bool:
    """meta.title 是否为"文件名退化形"（p_xxx/seg_xxx/fx_* / 空）——回填判定。"""
    t = (meta.get("title") or "").strip()
    return (not t) or bool(_STEM_TITLE.match(t)) or t == (meta.get("file") or "").rsplit(".", 1)[0]


def _load_kv_cache(paper_id: str, key: str) -> dict | None:
    rows = db.query("SELECT meta_json FROM papers WHERE id=?", (paper_id,))
    if not rows:
        return None
    try:
        mj = json.loads(rows[0]["meta_json"] or "{}")
    except Exception:  # noqa: BLE001
        return None
    v = mj.get(key)
    return v if isinstance(v, dict) else None


def _save_kv_cache(paper_id: str, key: str, value: dict) -> None:
    with db.tx() as c:
        rows = c.execute("SELECT meta_json FROM papers WHERE id=?", (paper_id,)).fetchall()
        if not rows:
            return
        try:
            mj = json.loads(rows[0]["meta_json"] or "{}")
        except Exception:  # noqa: BLE001
            mj = {}
        mj[key] = value
        c.execute("UPDATE papers SET meta_json=? WHERE id=?",
                  (json.dumps(mj, ensure_ascii=False), paper_id))


def _fetch_crossref(title: str, paper_id: str, timeout: float = 8.0) -> dict | None:
    """标题 → Crossref 查 {doi, venue, year}；归一化相似度 <0.6 或失败返回 None（静默）。

    结果（含空命中）缓存进 meta_json，重解析零重复请求。"""
    from difflib import SequenceMatcher

    t = _norm_text(title)
    if len(t) < 12:
        return None
    cache_key = _CROSSREF_CACHE_PREFIX + t
    cached = _load_kv_cache(paper_id, cache_key)
    if cached is not None:
        return cached or None
    try:
        resp = httpx.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": t, "rows": 3},
            timeout=timeout,
            headers={"User-Agent": "evitrace/1.0 (academic-assistant)"},
        )
        resp.raise_for_status()
        items = (resp.json().get("message", {}) or {}).get("items", [])
    except Exception as e:  # noqa: BLE001 —— 网络故障静默降级（空缓存防重复打）
        logger.warning(f"Crossref 查询失败: {type(e).__name__}: {e}", module="meta")
        _save_kv_cache(paper_id, cache_key, {})
        return None
    best: tuple[float, dict] | None = None
    for it in items:
        ct = (it.get("title") or [""])[0]
        ratio = SequenceMatcher(None, t, _norm_text(ct)).ratio()
        if ratio >= 0.6 and (best is None or ratio > best[0]):
            best = (ratio, it)
    result: dict = {}
    if best:
        it = best[1]
        year_parts = ((it.get("issued", {}) or {}).get("date-parts") or [[0]])
        result = {"doi": (it.get("DOI") or "").strip(),
                  "venue": ((it.get("container-title") or [""])[0] or "").strip(),
                  "year": int(year_parts[0][0] or 0)}
    _save_kv_cache(paper_id, cache_key, result)
    return result or None


# ---------- 编排：抽取 + 落库 ----------

def extract_and_store_metadata(paper_id: str, docir: dict) -> dict:
    """跑分层抽取，合并进 docir["meta"] 并更新 papers 行；返回最终 meta。

    合并规则：作者取「并集」——arXiv API 提供权威姓名列表，LLM 补 affiliation；
    abstract/venue/year/doi 优先 arXiv API，缺失处依次 Crossref/LLM/启发式补；
    标题为 stem 退化形时用 arXiv/LLM title 回填（细粒度 P0-2）。
    """
    meta = docir.setdefault("meta", {})
    arxiv_base = _base_of(meta.get("arxiv_id", ""))

    api_meta = None
    cached = _load_cache(paper_id, arxiv_base)
    if cached is not None:
        api_meta = cached
    elif arxiv_base:
        api_meta = fetch_arxiv_meta(arxiv_base)
        if api_meta is not None:
            _save_cache(paper_id, arxiv_base, api_meta)

    llm_meta = None
    if not api_meta or any(not a.get("affiliation") for a in api_meta["authors"]) \
            or not api_meta.get("abstract"):
        # LLM 兜顶：API 缺失、或作者缺机构、或缺摘要时才调用（省 token）
        llm_meta = llm_extract(first_page_text(docir))

    # 标题回填（stem 退化形 → 权威层 title）：papers.title 由 jobs 在本调用之后落库
    new_title = (api_meta or {}).get("title") or (llm_meta or {}).get("title", "")
    if new_title and _title_stem_like(meta):
        meta["title"] = new_title

    authors = _merge_authors(api_meta, llm_meta)
    abstract = (api_meta or {}).get("abstract") or (llm_meta or {}).get("abstract") \
        or heuristic_abstract(docir)
    venue = (api_meta or {}).get("journal_ref") or (llm_meta or {}).get("venue", "")
    year = (api_meta or {}).get("year") or (llm_meta or {}).get("year") or 0
    doi = (api_meta or {}).get("doi") or (llm_meta or {}).get("doi", "")
    # DOI 链（细粒度 P0-3）：首页正文正则 → arXiv 官方合成 DOI（确认 arXiv 文档时）
    if not doi:
        fp = first_page_text(docir, 3000)
        m_doi = _DOI_RE.search(fp)
        if m_doi:
            doi = m_doi.group(0).rstrip(".,;")
    if not doi and arxiv_base:
        doi = f"10.48550/arXiv.{arxiv_base}"
    # Crossref 兜底（用户选定外呼方案）：venue/doi 仍空且有可用标题时按题名查
    cr_meta = None
    lookup_title = (api_meta or {}).get("title") or (llm_meta or {}).get("title", "")
    if (not venue or not doi.startswith("10.48550/")) and lookup_title:
        cr_meta = _fetch_crossref(lookup_title, paper_id)
    if cr_meta:
        venue = venue or cr_meta.get("venue", "")
        if not doi or doi.startswith("10.48550/"):
            doi = cr_meta.get("doi", "") or doi
        year = year or cr_meta.get("year", 0)

    sources = []
    if api_meta:
        sources.append("arxiv_api")
    if cr_meta:
        sources.append("crossref")
    if llm_meta:
        sources.append("llm")
    if not sources:
        sources.append("heuristic")  # 全降级：只剩启发式/引擎结果
    meta.update({
        "authors": authors,
        "abstract": abstract,
        "venue": venue,
        "year": year,
        "doi": doi,
        "meta_source": "+".join(sources) if sources else "engine",
        "categories": (api_meta or {}).get("categories", meta.get("categories", [])),
    })

    with db.tx() as c:
        # doi/pub_year/venue 落独立列（9 层方案 L2）：标识符精确层与 year 特征的输入
        c.execute("UPDATE papers SET abstract=?, authors_json=?, meta_source=?, "
                  "doi=?, pub_year=?, venue=? WHERE id=?",
                  (abstract, json.dumps(authors, ensure_ascii=False),
                   meta.get("meta_source", ""), doi, int(year or 0), venue, paper_id))
    return meta


def _merge_authors(api_meta: dict | None, llm_meta: dict | None) -> list[dict]:
    api_authors = (api_meta or {}).get("authors") or []
    llm_authors = (llm_meta or {}).get("authors") or []
    if not api_authors:
        return llm_authors
    out = []
    for i, a in enumerate(api_authors):
        aff = a.get("affiliation") or ""
        if not aff and i < len(llm_authors):
            aff = llm_authors[i].get("affiliation", "")
        out.append({"name": a["name"], "affiliation": aff})
    return out


def _base_of(arxiv_id: str) -> str:
    m = re.match(r"^(\d{4}\.\d{4,5})", (arxiv_id or "").strip())
    return m.group(1) if m else ""


def _load_cache(paper_id: str, arxiv_base: str) -> dict | None:
    if not arxiv_base:
        return None
    rows = db.query("SELECT meta_json FROM papers WHERE id=?", (paper_id,))
    if not rows:
        return None
    try:
        mj = json.loads(rows[0]["meta_json"] or "{}")
    except Exception:  # noqa: BLE001
        return None
    cached = mj.get(_META_CACHE_PREFIX + arxiv_base)
    return cached if isinstance(cached, dict) else None


def _save_cache(paper_id: str, arxiv_base: str, meta: dict) -> None:
    with db.tx() as c:
        rows = c.execute("SELECT meta_json FROM papers WHERE id=?", (paper_id,)).fetchall()
        if not rows:
            return
        try:
            mj = json.loads(rows[0]["meta_json"] or "{}")
        except Exception:  # noqa: BLE001
            mj = {}
        mj[_META_CACHE_PREFIX + arxiv_base] = meta
        c.execute("UPDATE papers SET meta_json=? WHERE id=?",
                  (json.dumps(mj, ensure_ascii=False), paper_id))
