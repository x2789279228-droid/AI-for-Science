"""无幻觉论文框架（模块六 P0-2）：固定章节 + References 白名单 + 确定性后验。

核心防幻觉设计（不信任 LLM，只信代码校验）：
- 白名单只来自选中论文的 references 表 + 库内论文（Phase 0 后有真实
  authors/year/venue/doi 字段），绝不 LLM 生成文献条目；
- Round7 配额修复：按选中论文顺序每篇取前 20 条、总量 ≤60 且每篇保底 5 条
  （有则取）——旧实现 entries[:80] 会把第 3 篇选中论文的引用几乎全截掉；
- References 章节由代码从正文实际引用的 [R#] 确定性组装（无孤儿、无编造）；
- 每节起草后后验：非法 [R#] 剔除并记 guardrails；[E#] 缺证据 → 该句标警告；
- used<3 时用「补引」提示重试 1 次（只补 Intro/Related Work 引用句），
  仍不足记 guardrail refs_underused:N（前端如实标注，不强行凑引用）；
- 导出 MD：References 只列正文实际引用条目；未引用条目另列「未引用（未纳入
  正文）」且不带 [R#] 编号（旧实现把备选条目也印成 [R#] 编号，易被误认为正文引用）。
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
_MAX_REFS = 60            # 白名单总量上限（与 prompt 全量容量一致：≤60 条全部喂给 LLM）
_PER_PAPER_REFS = 20      # 单篇选中论文配额
_MIN_REFS_PER_PAPER = 5   # 每篇至少纳入条数（有则取）
_RETRY_MIN_USED = 3       # 正文引用低于此值 → 触发一次「补引」重试并如实记 guardrail


# ---------- References 白名单 ----------

def _entry_key(e: dict) -> str:
    """去重键：doi → arxiv_id → title_norm 三级回落（全库真实字段）。"""
    if e.get("doi"):
        return f"doi:{str(e['doi']).lower()}"
    if e.get("arxiv_id"):
        return f"arxiv:{e['arxiv_id']}"
    return f"t:{title_norm(e.get('title', ''))}"


def build_reference_whitelist(paper_ids: list[str]) -> list[dict]:
    """候选 = 选中论文的 references + 库内论文本身；doi→arxiv→title_norm 去重。

    Round7 配额：按 paper_ids 顺序，每篇最多 20 条、总量 ≤60；第一轮每篇保底
    5 条（有则取，靠前论文优先），第二轮按顺序补到每篇 20 条，最后用库内论文
    兜底填满。条目字段全部来自库内真实数据；缺失字段留空并标注（不编造）。"""
    keys: set[str] = set()
    per_paper: list[list[dict]] = []

    def _try_add(e: dict, bucket: list[dict]) -> bool:
        k = _entry_key(e)
        if not k or k == "t:" or k in keys:
            return False
        bucket.append(e)
        keys.add(k)
        return True

    for pid in paper_ids:
        bucket: list[dict] = []
        for r in db.query(
                'SELECT title, authors, year, venue, arxiv_id, doi FROM "references" '
                "WHERE paper_id=? AND title != '' ORDER BY idx", (pid,)):
            _try_add({"title": r["title"] or "", "authors": r["authors"] or "",
                      "year": r["year"] or 0, "venue": r["venue"] or "",
                      "arxiv_id": (r["arxiv_id"] or "").strip(),
                      "doi": r["doi"] or ""}, bucket)
            if len(bucket) >= _PER_PAPER_REFS:
                break
        per_paper.append(bucket)

    # 第一轮：每篇保底 5 条（配额耗尽时靠前论文优先，符合「按选中顺序」语义）
    counts = [0] * len(per_paper)
    total = 0
    for i, bucket in enumerate(per_paper):
        take = min(_MIN_REFS_PER_PAPER, len(bucket), max(_MAX_REFS - total, 0))
        counts[i] = take
        total += take
    # 第二轮：按选中顺序补到每篇 20 条（总量 ≤60）
    for i, bucket in enumerate(per_paper):
        extra = min(_PER_PAPER_REFS - counts[i], len(bucket) - counts[i],
                    max(_MAX_REFS - total, 0))
        counts[i] += extra
        total += extra
    entries: list[dict] = []
    for i, bucket in enumerate(per_paper):
        entries.extend(bucket[:counts[i]])

    # 第三轮：库内论文本身作兜底候选，直到总量配额用尽
    if len(entries) < _MAX_REFS:
        for p in db.query("SELECT id, title, arxiv_id, pub_year, venue FROM papers "
                          "WHERE status='active'"):
            _try_add({"title": p["title"] or "", "authors": "",
                      "year": p["pub_year"] or 0, "venue": p["venue"] or "",
                      "arxiv_id": (p["arxiv_id"] or "").strip(), "doi": "",
                      "paper_id": p["id"]}, entries)
            if len(entries) >= _MAX_REFS:
                break

    out = entries[:_MAX_REFS]
    for i, e in enumerate(out, 1):
        e["ref_id"] = i
        e["missing_fields"] = [f for f in ("authors", "year", "venue")
                               if not e.get(f)] if not e.get("paper_id") else []
    return out


def _ref_fields(e: dict) -> str:
    """条目字段渲染：真实字段，缺失标注（绝不补编）。"""
    parts = [e.get("title") or "（标题缺失）"]
    if e.get("authors"):
        parts.append(e["authors"])
    parts.append(f"({e['year']})" if e.get("year") else "(年份缺失)")
    if e.get("venue"):
        parts.append(e["venue"])
    if e.get("arxiv_id"):
        parts.append(f"arXiv:{e['arxiv_id']}")
    if e.get("doi"):
        parts.append(e["doi"])
    return ". ".join(parts)


def _ref_line(e: dict) -> str:
    """References 条目（仅正文实际引用条目才带 [R#] 编号）。"""
    return f"[R{e['ref_id']}] " + _ref_fields(e)


def _unused_line(e: dict) -> str:
    """未引用备选条目：无 [R#] 编号（避免被误读为正文引用）。"""
    return "- " + _ref_fields(e)


# ---------- 分节起草与后验 ----------

_R_MARK = re.compile(r"\[R(\d{1,3})\]")
_E_MARK = re.compile(r"\[E(\d{1,3})\]")

_REF_CONSTRAINT = ("引用约束：Related Work/Introduction 两节至少引用 5 个不同 [R#]，"
                   "且只能从上方白名单取，禁止编造任何文献条目或编号。")


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


def _retry_citation_boost(idea: str, whitelist: list[dict], drafted: list[dict],
                          wl_text: str) -> list[str]:
    """used<3 补引重试（仅 1 次）：只向 Introduction / Related Work 追补引用句。
    返回新增 guardrails；正文就地追加，且仍走 _sanitize_section——编造 [R#] 必被剔除。"""
    targets = [s for s in drafted
               if s["title"] in ("Introduction", "Related Work")
               and "待补" not in s["content"] and "LLM 不可用" not in s["content"]]
    if not targets:
        return []
    excerpt = "\n\n".join(f"## {s['title']}\n{s['content'][:1200]}" for s in targets)
    try:
        system, pv = _resolve(
            "copilot", "system_copilot_citation_boost",
            "你是学术写作助手。只为已有章节补充文献引用句，引用只能取自白名单 [R#]，禁止编造。")
        res = llm.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content":
                 f"研究 idea：{idea}\n\n已有章节：\n{excerpt}\n\n"
                 f"<references>\n{wl_text}\n</references>\n\n"
                 "任务：为 Introduction 与 Related Work 各补 1-2 句文献引用句，"
                 "至少引用 2 个不同 [R#]，只能从白名单取，禁止编造。"
                 "严格按两行输出：\nIntroduction: <补充句>\nRelated Work: <补充句>\n"
                 "不需要补充的节输出空行，不要重复原文。"}],
            temperature=0.2, max_tokens=300, module="copilot_writing_retry",
            audit_prompt=f"{idea[:80]}/refs_boost", prompt_version=pv)
        text = (res.get("text") or "").strip()
    except Exception:  # noqa: BLE001 —— 补引失败不阻断主流程
        return ["refs_boost_failed"]
    if not text:
        return ["refs_boost_empty"]

    parsed: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(Introduction|Related Work)\s*[:：]\s*(.+)$",
                     line, re.IGNORECASE)
        if m:
            parsed[m.group(1).strip().lower()] = m.group(2).strip()
    if not parsed:
        # 未按格式输出 → 整段并入 Related Work（仍经后验消毒，不会引入编造）
        first = next((s for s in targets if s["title"] == "Related Work"), targets[0])
        parsed[first["title"].lower()] = text[:600]

    guardrails: list[str] = []
    for s in targets:
        add = parsed.get(s["title"].lower())
        if not add:
            continue
        cleaned, g = _sanitize_section(add, whitelist, set())   # 补引句不允许 [E#]
        guardrails += g
        if cleaned.strip():
            s["content"] = f"{s['content']}\n\n{cleaned.strip()}"
            cited = sorted({int(m.group(1)) for m in _R_MARK.finditer(cleaned)})
            s["cited_refs"] = sorted(set(s.get("cited_refs") or []) | set(cited))
    return guardrails


def writing_framework(idea: str, paper_ids: list[str], *,
                      sections: list[str] | None = None, progress=None) -> dict:
    """生成论文框架：大纲 → 分节（证据包 → LLM 起草 → 后验）→ 确定性 References。"""
    from ..rag.answering import format_context

    secs = [s for s in (sections or DEFAULT_SECTIONS) if s] or DEFAULT_SECTIONS
    whitelist = build_reference_whitelist(paper_ids)
    if not whitelist:
        return {"error": "whitelist_empty:选中论文无可用的真实参考文献（先完成解析）"}

    valid_ids = {e["ref_id"] for e in whitelist}
    # Round7：白名单已 ≤60 条，全量喂给 LLM（旧实现只喂前 60 条，配套 80 条配额）
    wl_text = "\n".join(f"[R{e['ref_id']}] {e['title'][:80]}"
                        + (f"（{e['year']}）" if e.get("year") else "")
                        for e in whitelist)
    drafted: list[dict] = []
    guardrails_all: list[str] = []
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
                 f"<evidence>\n{context[:6000]}\n</evidence>\n{_REF_CONSTRAINT}\n请写该章节草稿。"}],
            temperature=0.2, max_tokens=800, module="copilot_writing",
            audit_prompt=f"{idea[:80]}/{name}", prompt_version=pv)
        text = (res.get("text") or "").strip() or "（生成失败，待补）"
        text, g = _sanitize_section(text, whitelist, set(eid_map.keys()))
        guardrails_all += g
        cited = sorted({int(m.group(1)) for m in _R_MARK.finditer(text)
                        if int(m.group(1)) in valid_ids})
        drafted.append({"title": name, "content": text, "cited_refs": cited,
                        "evidence_count": len(hits)})

    def _cited_now() -> set[int]:
        return {int(m.group(1)) for s in drafted for m in _R_MARK.finditer(s["content"])
                if int(m.group(1)) in valid_ids}

    cited_refs = _cited_now()
    used = [e for e in whitelist if e["ref_id"] in cited_refs]
    if len(used) < _RETRY_MIN_USED and llm.available():
        # 真实 LLM 归档 used=0~2 近乎空集 → 补引重试 1 次（只补 Intro / Related Work 引用句）
        guardrails_all += _retry_citation_boost(idea, whitelist, drafted, wl_text)
        cited_refs = _cited_now()
        used = [e for e in whitelist if e["ref_id"] in cited_refs]
    if len(used) < _RETRY_MIN_USED:
        guardrails_all.append(f"refs_underused:{len(used)}")   # 前端如实展示「引用不足」

    unused = [e for e in whitelist if e["ref_id"] not in cited_refs]
    references_md = "\n".join(_ref_line(e) for e in used) or "（正文暂未引用任何白名单文献）"
    drafted.append({"title": "References", "content": references_md,
                    "cited_refs": sorted(cited_refs), "deterministic": True})

    outline = {"idea": idea, "sections": secs, "papers": len(paper_ids),
               "whitelist_size": len(whitelist), "refs_used": len(used)}
    content_md = (f"# {idea[:60]}\n\n"
                  + "\n\n".join(f"## {s['title']}\n\n{s['content']}" for s in drafted))
    if unused:
        # 未引用条目单独成节、不带 [R#] 编号（避免导出稿被误读为正文引用）
        shown = unused[:20]
        content_md += ("\n\n## 未引用（未纳入正文）\n\n"
                       + "\n".join(_unused_line(e) for e in shown))
        if len(unused) > len(shown):
            content_md += f"\n\n（另有 {len(unused) - len(shown)} 条未引用白名单条目未展示）"
    return {"outline": outline, "sections": drafted,
            "references": {"used": used, "unused_count": len(unused),
                           "whitelist_size": len(whitelist)},
            "content_md": content_md, "guardrails": guardrails_all}
