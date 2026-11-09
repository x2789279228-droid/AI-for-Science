"""无幻觉论文框架（模块六 P0-2）：固定章节 + References 白名单 + 确定性后验。

核心防幻觉设计（不信任 LLM，只信代码校验）：
- 白名单只来自选中论文的 references 表 + 库内论文（Phase 0 后有真实
  authors/year/venue/doi 字段），绝不 LLM 生成文献条目；
- References 章节由代码从正文实际引用的 [R#] 确定性组装（无孤儿、无编造）；
- 每节起草后后验：非法 [R#] 剔除并记 guardrails；[E#] 缺证据 → 该句标警告；
- 证据 < 3 的节如实写「待补：证据不足」（沿用 Writer 拒写语义）。
"""
from __future__ import annotations

import re

from .. import db
from ..llm import client as llm
from ..llm.prompts import resolve as _resolve
from ..rag.discovery import title_norm
from ..rag.indexing import hybrid_search

DEFAULT_SECTIONS = ["Abstract", "Introduction", "Related Work", "Method",
                    "Experiments", "Conclusion"]
_MIN_EVIDENCE = 3
_MAX_REFS = 80


# ---------- References 白名单 ----------

def build_reference_whitelist(paper_ids: list[str]) -> list[dict]:
    """候选 = 选中论文的 references + 库内论文本身；doi→arxiv→title_norm 去重。
    条目字段全部来自库内真实数据；缺失字段留空并标注（不编造）。"""
    entries: list[dict] = []
    keys: set[str] = set()

    def _add(e: dict) -> None:
        key = (f"doi:{(e.get('doi') or '').lower()}" if e.get("doi")
               else f"arxiv:{e.get('arxiv_id')}" if e.get("arxiv_id")
               else f"t:{title_norm(e.get('title', ''))}")
        if not key or key == "t:" or key in keys:
            return
        keys.add(key)
        entries.append(e)

    for pid in paper_ids:
        for r in db.query(
                'SELECT title, authors, year, venue, arxiv_id, doi FROM "references" '
                "WHERE paper_id=? AND title != '' ORDER BY idx", (pid,)):
            _add({"title": r["title"] or "", "authors": r["authors"] or "",
                  "year": r["year"] or 0, "venue": r["venue"] or "",
                  "arxiv_id": (r["arxiv_id"] or "").strip(), "doi": r["doi"] or ""})
    for p in db.query("SELECT id, title, arxiv_id, pub_year, venue FROM papers "
                      "WHERE status='active'"):
        _add({"title": p["title"] or "", "authors": "", "year": p["pub_year"] or 0,
              "venue": p["venue"] or "", "arxiv_id": (p["arxiv_id"] or "").strip(),
              "doi": "", "paper_id": p["id"]})
    out = entries[:_MAX_REFS]
    for i, e in enumerate(out, 1):
        e["ref_id"] = i
        e["missing_fields"] = [f for f in ("authors", "year", "venue")
                               if not e.get(f)] if not e.get("paper_id") else []
    return out


def _ref_line(e: dict) -> str:
    """References 条目渲染：真实字段，缺失标注（绝不补编）。"""
    parts = [e.get("title") or "（标题缺失）"]
    if e.get("authors"):
        parts.append(e["authors"])
    year = f"({e['year']})" if e.get("year") else "(年份缺失)"
    parts.append(year)
    if e.get("venue"):
        parts.append(e["venue"])
    if e.get("arxiv_id"):
        parts.append(f"arXiv:{e['arxiv_id']}")
    if e.get("doi"):
        parts.append(e["doi"])
    return f"[R{e['ref_id']}] " + ". ".join(parts)


# ---------- 分节起草与后验 ----------

_R_MARK = re.compile(r"\[R(\d{1,3})\]")
_E_MARK = re.compile(r"\[E(\d{1,3})\]")


def _sanitize_section(text: str, whitelist: list[dict], eids: set[int]) -> tuple[str, list[str]]:
    """确定性后验：非法 [R#]（白名单外）整段标记剔除；[E#] 超界标记。"""
    guardrails: list[str] = []
    valid = {e["ref_id"] for e in whitelist}

    def _sub_r(m: re.Match) -> str:
        n = int(m.group(1))
        if n in valid:
            return m.group(0)
        guardrails.append(f"invalid_ref_R{n}_removed")
        return "（引用已剔除：白名单外）"

    text = _R_MARK.sub(_sub_r, text)

    def _sub_e(m: re.Match) -> str:
        n = int(m.group(1))
        if n in eids:
            return m.group(0)
        guardrails.append(f"invalid_evidence_E{n}_removed")
        return "（证据标记已剔除：超出证据包）"

    return _E_MARK.sub(_sub_e, text), guardrails


def writing_framework(idea: str, paper_ids: list[str], *,
                      sections: list[str] | None = None, progress=None) -> dict:
    """生成论文框架：大纲 → 分节（证据包 → LLM 起草 → 后验）→ 确定性 References。"""
    from ..rag.answering import format_context

    secs = [s for s in (sections or DEFAULT_SECTIONS) if s] or DEFAULT_SECTIONS
    whitelist = build_reference_whitelist(paper_ids)
    if not whitelist:
        return {"error": "whitelist_empty:选中论文无可用的真实参考文献（先完成解析）"}

    wl_text = "\n".join(f"[R{e['ref_id']}] {e['title'][:80]}"
                        + (f"（{e['year']}）" if e.get("year") else "")
                        for e in whitelist[:60])
    drafted: list[dict] = []
    guardrails_all: list[str] = []
    cited_refs: set[int] = set()
    evidence_by_section: dict[str, dict] = {}

    for name in secs:
        if progress:
            progress(name)
        hits = hybrid_search(paper_ids, f"{idea} {name}", top_k=5).get("hits", [])
        if len(hits) < _MIN_EVIDENCE:
            drafted.append({"title": name, "content": "待补：证据不足（库内相关证据 < 3 条，"
                            "宁缺毋滥——请补充相关论文入库或缩小 idea 范围）",
                            "cited_refs": [], "evidence_count": len(hits)})
            continue
        context, eid_map = format_context(hits)
        evidence_by_section[name] = {"context": context, "eid_map": eid_map}
        if not llm.available():
            drafted.append({"title": name, "content": "（LLM 不可用，正文待生成；证据已就绪）",
                            "cited_refs": [], "evidence_count": len(hits)})
            continue
        system, pv = _resolve("copilot", "system_copilot_section",
                              "你是学术论文写作助手。只写证据支撑的内容，引用只用白名单 [R#]。")
        res = llm.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content":
                 f"研究 idea：{idea}\n章节：{name}\n\n<references>\n{wl_text}\n</references>\n\n"
                 f"<evidence>\n{context[:6000]}\n</evidence>\n请写该章节草稿。"}],
            temperature=0.2, max_tokens=800, module="copilot_writing",
            audit_prompt=f"{idea[:80]}/{name}", prompt_version=pv)
        text = (res.get("text") or "").strip() or "（生成失败，待补）"
        text, g = _sanitize_section(text, whitelist, set(eid_map.keys()))
        guardrails_all += g
        cited = sorted({int(m.group(1)) for m in _R_MARK.finditer(text)
                        if int(m.group(1)) in {e["ref_id"] for e in whitelist}})
        cited_refs.update(cited)
        drafted.append({"title": name, "content": text, "cited_refs": cited,
                        "evidence_count": len(hits)})

    used = [e for e in whitelist if e["ref_id"] in cited_refs]
    unused = [e for e in whitelist if e["ref_id"] not in cited_refs]
    references_md = "\n".join(_ref_line(e) for e in used) or "（正文暂未引用任何白名单文献）"
    drafted.append({"title": "References", "content": references_md,
                    "cited_refs": sorted(cited_refs), "deterministic": True})

    outline = {"idea": idea, "sections": secs, "papers": len(paper_ids),
               "whitelist_size": len(whitelist)}
    content_md = (f"# {idea[:60]}\n\n"
                  + "\n\n".join(f"## {s['title']}\n\n{s['content']}" for s in drafted)
                  + "\n\n## 未引用的白名单条目（备选）\n\n"
                  + ("\n".join(_ref_line(e) for e in unused[:20]) or "（无）"))
    return {"outline": outline, "sections": drafted,
            "references": {"used": used, "unused_count": len(unused),
                           "whitelist_size": len(whitelist)},
            "content_md": content_md, "guardrails": guardrails_all}
