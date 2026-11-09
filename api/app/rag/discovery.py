"""多源检索适配器（模块四 P0-1）：arXiv / Semantic Scholar / OpenAlex 统一候选面。

设计要点：
- 统一 PaperCandidate：{title, authors, year, venue, doi, arxiv_id, citation_count,
  abstract, source(多源合并列表), pdf_url}——citation_count/venue 供引用含金量展示
  与推荐打分复用；
- 去重链 doi → arxiv_id → title_norm（与 references 表同口径）；
- 断网兜底：沿用 tools._tool_cache_* 模式，失败回放上次成功结果并如实标注
  source=cache_offline；
- 限速礼貌：S2 免 key 共享池（~1rps，429 单次退避重试）、OpenAlex 完全免 key。
选型理由：三家均免 key/免注册 JSON REST，覆盖 arXiv 预印本（原文下载）+ S2
（引用数/venue/batch 元数据补全）+ OpenAlex（开放全量、稳定不限速）。
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx

from ..agents.tools import _tool_cache_read, _tool_cache_write

_UA = {"User-Agent": "meridian-scholar-agent/1.0"}
_S2_FIELDS = "title,authors,year,venue,citationCount,externalIds,abstract,openAccessPdf"


def title_norm(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (title or "").lower())).strip()


# ---------- 单源 Provider：统一返回 {ok, entries, source, error?} ----------

def _arxiv_provider(query: str, limit: int) -> dict:
    from ..agents.tools import _arxiv_search
    r = _arxiv_search(query, max_results=limit)
    entries = [{
        "title": e["title"], "authors": e.get("authors", []), "year": (e.get("published") or "")[:4],
        "venue": "", "doi": "", "arxiv_id": e.get("arxiv_id", ""),
        "citation_count": None, "abstract": e.get("abstract", ""),
        "sources": [f"arxiv({'cache' if r.get('source') != 'live' else 'live'})"],
        "pdf_url": e.get("pdf_url", ""),
    } for e in r.get("entries", [])]
    return {"ok": bool(r.get("ok")), "entries": entries,
            "source": r.get("source", "none"), "error": r.get("error")}


def _s2_entries(payload: dict) -> list[dict]:
    out = []
    for p in payload.get("data") or []:
        ext = p.get("externalIds") or {}
        oa = (p.get("openAccessPdf") or {}).get("url", "")
        out.append({
            "title": p.get("title") or "", "authors": [a.get("name", "") for a in (p.get("authors") or [])][:8],
            "year": p.get("year") or 0, "venue": p.get("venue") or "",
            "doi": ext.get("DOI", ""), "arxiv_id": ext.get("ArXiv", ""),
            "citation_count": p.get("citationCount", 0), "abstract": (p.get("abstract") or "")[:600],
            "sources": ["s2"], "pdf_url": oa,
        })
    return out


def _s2_provider(query: str, limit: int) -> dict:
    """Semantic Scholar Graph API（免 key 共享池；429 单次退避重试）。"""
    try:
        for attempt in range(2):
            resp = httpx.get("https://api.semanticscholar.org/graph/v1/paper/search",
                             params={"query": query, "limit": max(1, min(limit, 20)),
                                     "fields": _S2_FIELDS},
                             headers=_UA, timeout=25)
            if resp.status_code == 429 and attempt == 0:
                time.sleep(2.0)
                continue
            resp.raise_for_status()
            entries = _s2_entries(resp.json())
            _tool_cache_write("s2", query, {"entries": entries})
            return {"ok": True, "entries": entries, "source": "live"}
    except Exception as e:  # noqa: BLE001 —— 断网回放缓存
        cached = _tool_cache_read("s2", query)
        if cached:
            return {"ok": True, "entries": cached.get("entries", []), "source": "cache_offline",
                    "error": f"{type(e).__name__}: {e}"[:120]}
        return {"ok": False, "entries": [], "source": "none", "error": f"{type(e).__name__}: {e}"[:120]}
    return {"ok": False, "entries": [], "source": "none", "error": "s2_unknown"}


def _abstract_from_inverted(idx: dict | None) -> str:
    if not idx:
        return ""
    pos: list[tuple[int, str]] = []
    for word, spots in idx.items():
        pos.extend((i, word) for i in spots)
    return " ".join(w for _, w in sorted(pos))[:600]


def _openalex_provider(query: str, limit: int) -> dict:
    """OpenAlex works?search=（完全免 key；mailto 进礼貌池更稳）。"""
    try:
        resp = httpx.get("https://api.openalex.org/works",
                         params={"search": query, "per-page": max(1, min(limit, 20)),
                                 "mailto": "scholar-agent@example.org"},
                         headers=_UA, timeout=25)
        resp.raise_for_status()
        entries = []
        for w in resp.json().get("results") or []:
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            loc = w.get("primary_location") or {}
            src = (loc.get("source") or {}).get("display_name", "") if loc.get("source") else ""
            entries.append({
                "title": w.get("display_name") or "",
                "authors": [a.get("author", {}).get("display_name", "")
                            for a in (w.get("authorships") or [])[:8]],
                "year": w.get("publication_year") or 0, "venue": src,
                "doi": doi, "arxiv_id": "", "citation_count": w.get("cited_by_count", 0),
                "abstract": _abstract_from_inverted(w.get("abstract_inverted_index")),
                "sources": ["openalex"], "pdf_url": (loc.get("pdf_url") or ""),
            })
        _tool_cache_write("openalex", query, {"entries": entries})
        return {"ok": True, "entries": entries, "source": "live"}
    except Exception as e:  # noqa: BLE001
        cached = _tool_cache_read("openalex", query)
        if cached:
            return {"ok": True, "entries": cached.get("entries", []), "source": "cache_offline",
                    "error": f"{type(e).__name__}: {e}"[:120]}
        return {"ok": False, "entries": [], "source": "none", "error": f"{type(e).__name__}: {e}"[:120]}


PROVIDERS: dict[str, Any] = {"arxiv": _arxiv_provider, "s2": _s2_provider,
                             "openalex": _openalex_provider}


# ---------- 多源合并与去重 ----------

def _dedup(entries: list[dict]) -> list[dict]:
    """去重链 doi → arxiv_id → title_norm（三索引并查：同一论文的不同标识——
    arXiv 源带 arxiv_id、S2 源带 DOI——必须相遇合并）；非空字段互补、引用数取最大。"""
    by_doi: dict[str, int] = {}
    by_arxiv: dict[str, int] = {}
    by_title: dict[str, int] = {}
    groups: list[dict] = []

    def _absorb(kept: dict, e: dict) -> None:
        for f in ("venue", "doi", "arxiv_id", "pdf_url", "abstract"):
            if not kept.get(f) and e.get(f):
                kept[f] = e[f]
        if (e.get("citation_count") or 0) > (kept.get("citation_count") or 0):
            kept["citation_count"] = e["citation_count"]
        if not kept.get("year") and e.get("year"):
            kept["year"] = e["year"]
        if len(e.get("authors") or []) > len(kept.get("authors") or []):
            kept["authors"] = e["authors"]
        for s in e.get("sources", []):
            if s not in kept["sources"]:
                kept["sources"].append(s)

    for e in entries:
        doi = (e.get("doi") or "").lower()
        ax = e.get("arxiv_id") or ""
        tn = title_norm(e.get("title", ""))
        hit = None
        if doi and doi in by_doi:
            hit = by_doi[doi]
        elif ax and ax in by_arxiv:
            hit = by_arxiv[ax]
        elif tn and tn in by_title:
            hit = by_title[tn]
        if hit is None:
            gi = len(groups)
            groups.append({**e, "sources": list(e.get("sources", []))})
            if doi:
                by_doi[doi] = gi
            if ax:
                by_arxiv[ax] = gi
            if tn:
                by_title[tn] = gi
        else:
            _absorb(groups[hit], e)
            if doi and doi not in by_doi:
                by_doi[doi] = hit
            if ax and ax not in by_arxiv:
                by_arxiv[ax] = hit
    return groups


def search_multi(query: str, sources: list[str] | None = None, limit: int = 8) -> dict:
    """多源检索 + 去重。三源并行（不同主机，礼貌且时延 ≯ 单源最慢者）。
    返回 {entries, per_source: {name: ok/source/error}}。"""
    import sys
    from concurrent.futures import ThreadPoolExecutor

    mod = sys.modules[__name__]
    names = [s for s in (sources or ["arxiv", "s2", "openalex"])
             if hasattr(mod, f"_{s}_provider")]
    entries: list[dict] = []
    per: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
        futs = {name: pool.submit(getattr(mod, f"_{name}_provider"), query, limit)
                for name in names}
        for name, fut in futs.items():
            try:
                r = fut.result(timeout=60)
            except Exception as e:  # noqa: BLE001 —— 单源失败不拖垮其余
                r = {"ok": False, "entries": [], "source": "none",
                     "error": f"{type(e).__name__}: {e}"[:120]}
            per[name] = {"ok": bool(r.get("ok")), "source": r.get("source", "none"),
                         "error": r.get("error") or ""}
            if r.get("ok"):
                entries.extend(r["entries"])
    return {"entries": _dedup(entries)[: max(1, min(limit * 3, 40))], "per_source": per}


# ---------- 标识符精确解析（标题 / DOI / arXiv id） ----------

def resolve_identifier(*, title: str = "", doi: str = "", arxiv_id: str = "") -> dict:
    """精确解析：优先 S2 /paper/{id:DOI|ARXIV}，标题走 /paper/search 精确匹配；
    失败回退 OpenAlex filter。返回统一单候选（或 {ok: False}）。"""
    ident = ""
    if doi:
        ident = f"DOI:{doi.strip()}"
    elif arxiv_id:
        ident = f"ARXIV:{arxiv_id.strip().split('v')[0]}"
    try:
        if ident:
            resp = httpx.get(f"https://api.semanticscholar.org/graph/v1/paper/{ident}",
                             params={"fields": _S2_FIELDS}, headers=_UA, timeout=25)
            resp.raise_for_status()
            return {"ok": True, "entries": _s2_entries({"data": [resp.json()]}), "source": "s2"}
        if title:
            resp = httpx.get("https://api.semanticscholar.org/graph/v1/paper/search",
                             params={"query": title, "limit": 5, "fields": _S2_FIELDS},
                             headers=_UA, timeout=25)
            resp.raise_for_status()
            cands = _s2_entries(resp.json())
            exact = [c for c in cands if title_norm(c["title"]) == title_norm(title)]
            if exact or cands:
                return {"ok": True, "entries": exact or cands[:1], "source": "s2"}
    except Exception as e:  # noqa: BLE001 —— S2 失败回退 OpenAlex
        try:
            params: dict[str, Any] = {"per-page": 3, "mailto": "scholar-agent@example.org"}
            if doi:
                params["filter"] = f"doi:{doi}"
            elif title:
                params["search"] = title
            resp = httpx.get("https://api.openalex.org/works", params=params,
                             headers=_UA, timeout=25)
            resp.raise_for_status()
            results = resp.json().get("results") or []
            if results:
                w = results[0]
                return {"ok": True, "entries": [{
                    "title": w.get("display_name") or "", "year": w.get("publication_year") or 0,
                    "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name", ""),
                    "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
                    "citation_count": w.get("cited_by_count", 0),
                    "abstract": _abstract_from_inverted(w.get("abstract_inverted_index")),
                    "sources": ["openalex"], "pdf_url": (w.get("primary_location") or {}).get("pdf_url") or "",
                    "arxiv_id": "", "authors": []}], "source": "openalex"}
        except Exception as e2:  # noqa: BLE001
            return {"ok": False, "error": f"s2:{type(e).__name__}; openalex:{type(e2).__name__}"}
    return {"ok": False, "error": "not_found"}


# ---------- 参考文献列表导入（BibTeX / RIS / 纯文本 DOI·arXiv·标题行） ----------

_ARXIV_LINE = re.compile(r"(?:arxiv[:\s]*)?(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
_DOI_LINE = re.compile(r"10\.\d{4,9}/[\w.\-/:]+", re.IGNORECASE)


def parse_reference_list(text: str) -> dict:
    """解析用户提供的参考文献列表 → 待解析条目 [{raw, doi?, arxiv_id?, title?}]。
    自动识别：BibTeX（bibtexparser）、RIS（TY/TI/AU/DO 行）、纯文本（逐行 DOI/arXiv/标题）。"""
    text = (text or "").strip()
    if not text:
        return {"kind": "empty", "items": []}
    if "@" in text and re.search(r"@\w+\s*\{", text):
        try:
            import bibtexparser
            # bibtexparser 2.x 用 parse_string；1.x 用 loads——双 API 兼容
            if hasattr(bibtexparser, "parse_string"):
                db = bibtexparser.parse_string(text)
                raw_entries = [{k: v.value for k, v in e.fields_dict.items()}
                               for e in db.entries]
            else:
                db = bibtexparser.loads(text)
                raw_entries = db.entries
            items = []
            for raw in raw_entries:
                def _f(key: str, _e: dict = raw) -> str:
                    return str(_e.get(key) or "").strip()
                ax_src = _f("url") or _f("eprint")
                m_ax = _ARXIV_LINE.search(ax_src)
                items.append({"raw": (_f("title"))[:200],
                              "title": _f("title").strip(" {}"),
                              "doi": _f("doi"),
                              "arxiv_id": m_ax.group(1) if m_ax else "",
                              "year": int(_f("year")) if _f("year").isdigit() else 0})
            if items:
                return {"kind": "bibtex", "items": items}
        except Exception:  # noqa: BLE001,S110 —— BibTeX 解析失败回退纯文本（格式自识别）
            pass
    if re.search(r"^TY\s{2}[-–]\s", text, re.MULTILINE):
        items, cur = [], None
        for line in text.splitlines():
            tag = line[:2].upper()
            val = re.split(r"[-–]\s*", line[2:].strip() or "", maxsplit=1)
            content = val[1] if len(val) == 2 else line[6:].strip()
            if tag == "TY":
                cur = {"raw": "", "title": "", "doi": "", "arxiv_id": "", "year": 0}
            elif tag == "TI" and cur:
                cur["title"] = content
                cur["raw"] = content[:200]
            elif tag == "DO" and cur:
                cur["doi"] = content
            elif tag == "PY" and cur and content[:4].isdigit():
                cur["year"] = int(content[:4])
            elif tag == "ER" and cur:
                items.append(cur)
                cur = None
        if items:
            return {"kind": "ris", "items": items}
    items = []
    for line in (l.strip() for l in text.splitlines()):
        if len(line) < 6:
            continue
        m_doi, m_ax = _DOI_LINE.search(line), _ARXIV_LINE.search(line)
        items.append({"raw": line[:200],
                      "title": "" if (m_doi or m_ax) else line,
                      "doi": m_doi.group(0) if m_doi else "",
                      "arxiv_id": m_ax.group(1) if m_ax else "", "year": 0})
    return {"kind": "plain", "items": items}


def s2_metadata_batch(arxiv_ids: list[str], *, batch_size: int = 20) -> dict[str, dict]:
    """arXiv id 批量查 S2 元数据（citation_count/venue/abstract）——推荐懒补用。

    返回 {arxiv_id: {...}}；任一批失败即停（调用方按已有部分结果回填，缺者保持
    NULL 如实降级）。与 resolve_reference_items 同通道同字段，仅供内部轻量调用。
    """
    out: dict[str, dict] = {}
    for k in range(0, len(arxiv_ids), batch_size):
        chunk = arxiv_ids[k:k + batch_size]
        try:
            resp = httpx.post("https://api.semanticscholar.org/graph/v1/paper/batch",
                              params={"fields": _S2_FIELDS},
                              json={"ids": [f"ARXIV:{a}" for a in chunk]},
                              headers=_UA, timeout=40)
            resp.raise_for_status()
            for a, p in zip(chunk, resp.json()):
                if p:
                    e = _s2_entries({"data": [p]})[0]
                    out[a] = {"citation_count": e.get("citation_count"),
                              "venue": e.get("venue") or "",
                              "abstract": e.get("abstract") or "",
                              "year": e.get("year") or 0}
        except Exception:  # noqa: BLE001 —— 限速/断网：已取到的部分生效，其余如实缺省
            break
    return out


def resolve_reference_items(items: list[dict], *, batch_size: int = 20) -> list[dict]:
    """批量补全候选（S2 batch 优先；逐条 resolve 回退）。返回 PaperCandidate 列表，
    未解析条目如实标 resolved=False（不冒充）。"""
    ids = []
    for it in items:
        if it.get("doi"):
            ids.append(f"DOI:{it['doi']}")
        elif it.get("arxiv_id"):
            ids.append(f"ARXIV:{it['arxiv_id'].split('v')[0]}")
        else:
            ids.append(None)
    out: list[dict] = [None] * len(items)  # type: ignore[list-item]
    # S2 batch（免逐条限速）
    want = [i for i, x in enumerate(ids) if x]
    for k in range(0, len(want), batch_size):
        chunk = want[k:k + batch_size]
        try:
            resp = httpx.post("https://api.semanticscholar.org/graph/v1/paper/batch",
                              params={"fields": _S2_FIELDS},
                              json={"ids": [ids[i] for i in chunk]},
                              headers=_UA, timeout=40)
            resp.raise_for_status()
            for i, p in zip(chunk, resp.json()):
                if p:
                    out[i] = _s2_entries({"data": [p]})[0]
        except Exception:  # noqa: BLE001,S110 —— batch 失败走逐条回退（下一循环兜住）
            pass
    # 标题行逐条解析
    for i, it in enumerate(items):
        if out[i] is None and (it.get("title") or it.get("arxiv_id") or it.get("doi")):
            r = resolve_identifier(title=it.get("title", ""), doi=it.get("doi", ""),
                                   arxiv_id=it.get("arxiv_id", ""))
            if r.get("ok") and r.get("entries"):
                out[i] = r["entries"][0]
    result = []
    for it, cand in zip(items, out):
        if cand:
            result.append({**cand, "raw": it.get("raw", ""), "resolved": True})
        else:
            result.append({"title": it.get("title") or it.get("raw", ""), "authors": [],
                           "year": it.get("year", 0), "venue": "", "doi": it.get("doi", ""),
                           "arxiv_id": it.get("arxiv_id", ""), "citation_count": None,
                           "abstract": "", "sources": ["unresolved"], "pdf_url": "",
                           "raw": it.get("raw", ""), "resolved": False})
    return result
