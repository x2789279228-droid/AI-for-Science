"""DiffLens：版本演进差异总结（模块一 P0，对应赛题「自动对比并总结主要修订差异」）。

四步（方案 §1.3 既定）：
1. 章节对齐：标题归一相等 → SequenceMatcher 顺序约束对齐（ratio≥0.8 视为改名的同节）；
2. 段落 diff：对齐章节内 SequenceMatcher(段落列表) opcode → added/removed/replaced，
   replaced 附字符级高亮区间；References 类章节只计数不逐段（降级预案）；
3. 数值变化：数值 token（100K/300K/小数/%）集合差，带所属章节上下文；
4. LLM 摘要：Top diff 喂 chat → 3~5 条要点（结构/内容/数值/图表/结论 分型）；
   LLM 不可用时抽取式兜底，闭环不中断。

输出载荷（version_diffs.payload_json，前端 DiffLens 直接渲染）——
sections 元信息全量保留，段落明细只保留变化最重的 TOP_SECTIONS 章（其余 status 汇总），
对应降级预案「只展示 Top N 高置信差异 + 一句共 N 处变更」。
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from .. import db
from ..config import DOCIR_DIR
from ..llm import client as llm
from ..log import logger

TOP_SECTIONS = 20          # 段落明细保留的章节数上限
_REF_SECTION = re.compile(r"^(references|bibliography|文献)", re.IGNORECASE)
_NUM = re.compile(r"\d[\d,\.]*\s*(?:[KkMmBb](?:\s*[a-zA-Z]+)?|%|万|亿)?")


def load_docir_for(paper_id: str) -> dict | None:
    """按 DB docir_path 读 DocIR，容器内宿主绝对路径失效时回退数据目录（与路由同防御）。"""
    rows = db.query("SELECT docir_path FROM papers WHERE id=?", (paper_id,))
    path: Path | None = None
    if rows and rows[0]["docir_path"]:
        p = Path(rows[0]["docir_path"])
        path = p if p.exists() else None
    if path is None:
        path = DOCIR_DIR / f"{paper_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (t or "").lower())


# ---------- 1. 章节对齐 ----------

def align_sections(base_toc: list[dict], target_toc: list[dict]) -> list[dict]:
    """顺序约束对齐 → [{status: aligned|added|removed, title, base_page, target_page}]。"""
    b_norm = [_norm_title(t.get("title", "")) for t in base_toc]
    t_norm = [_norm_title(t.get("title", "")) for t in target_toc]
    sm = difflib.SequenceMatcher(None, b_norm, t_norm, autojunk=False)
    out: list[dict] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out.append({"status": "aligned", "title": target_toc[j1 + k]["title"],
                            "base_title": base_toc[i1 + k]["title"],
                            "base_page": base_toc[i1 + k].get("page"),
                            "target_page": target_toc[j1 + k].get("page")})
        elif tag == "replace":
            # 同长度段优先按 ratio≥0.8 配对（章节改名/序号漂移）；余量按 added/removed
            pairs = _fuzzy_pairs(b_norm[i1:i2], t_norm[j1:j2])
            for bi, ti, ratio in pairs:
                out.append({"status": "aligned", "title": target_toc[j1 + ti]["title"],
                            "base_title": base_toc[i1 + bi]["title"],
                            "base_page": base_toc[i1 + bi].get("page"),
                            "target_page": target_toc[j1 + ti].get("page")})
            used_b = {bi for bi, _, _ in pairs}
            used_t = {ti for _, ti, _ in pairs}
            for bi in range(i2 - i1):
                if bi not in used_b:
                    out.append({"status": "removed", "title": base_toc[i1 + bi]["title"],
                                "base_title": base_toc[i1 + bi]["title"],
                                "base_page": base_toc[i1 + bi].get("page"),
                                "target_page": None})
            for ti in range(j2 - j1):
                if ti not in used_t:
                    out.append({"status": "added", "title": target_toc[j1 + ti]["title"],
                                "base_title": "",
                                "base_page": None,
                                "target_page": target_toc[j1 + ti].get("page")})
        elif tag == "delete":
            for k in range(i1, i2):
                out.append({"status": "removed", "title": base_toc[k]["title"],
                            "base_title": base_toc[k]["title"],
                            "base_page": base_toc[k].get("page"), "target_page": None})
        else:  # insert
            for k in range(j1, j2):
                out.append({"status": "added", "title": target_toc[k]["title"],
                            "base_title": "", "base_page": None,
                            "target_page": target_toc[k].get("page")})
    return out


def _fuzzy_pairs(a_list: list[str], b_list: list[str]) -> list[tuple[int, int, float]]:
    pairs: list[tuple[int, int, float]] = []
    for bi, a in enumerate(a_list):
        best_j, best_r = -1, 0.0
        for ti, b in enumerate(b_list):
            if ti in {t for _, t, _ in pairs}:
                continue
            r = difflib.SequenceMatcher(None, a, b).ratio()
            if r > best_r:
                best_j, best_r = ti, r
        if best_j >= 0 and best_r >= 0.72:  # 「2 Training」→「2 Training (Revised)」≈0.78
            pairs.append((bi, best_j, best_r))
    return pairs


# ---------- 2/3. 段落 diff + 数值变化 ----------

def _paras_for_section(docir: dict, title: str, page) -> list[str]:
    """取某章节的段落文本列表：优先 section_path 匹配，退化为页码区间。"""
    blocks = docir.get("blocks", [])
    hits = [b for b in blocks if (b.get("section_path") or "") == title]
    if not hits and page is not None:
        pages_after = sorted({b.get("page", 0) for b in blocks if b.get("page", 0) > page})
        p_end = pages_after[0] if pages_after else page + 1
        hits = [b for b in blocks if b.get("page", 0) == page or
                (page < b.get("page", 0) < p_end)]
    return [(b.get("text") or "").strip() for b in hits
            if (b.get("text") or "").strip() and b.get("type") != "heading"]


def para_diff(base_docir: dict, target_docir: dict, section: dict) -> dict:
    """单章节段落级 diff：{...section, para_count, paras: [...], numbers: [...]}。

    References 类章节只计数（降级预案：先 diff 正文+章节标题，References 单独处理）。
    """
    title = section.get("base_title") or section.get("title", "")
    is_ref = bool(_REF_SECTION.match(title.strip()))
    base_paras = _paras_for_section(base_docir, title, section.get("base_page"))
    target_paras = _paras_for_section(target_docir, section.get("title", ""),
                                      section.get("target_page"))
    entry = {**section, "para_count": (len(base_paras), len(target_paras)),
             "is_references": is_ref, "paras": [], "numbers": [],
             "changed": 0}
    if is_ref:
        if len(base_paras) != len(target_paras):
            entry["changed"] = 1
            entry["paras"] = [{"op": "refs_changed",
                               "base": f"{len(base_paras)} 条", "target": f"{len(target_paras)} 条"}]
        return entry

    numbers: list[dict] = []
    changed = 0
    paras: list[dict] = []
    sm = difflib.SequenceMatcher(None, base_paras, target_paras, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        changed += 1
        if tag in ("replace",):
            for k in range(max(i2 - i1, j2 - j1)):
                b_txt = base_paras[i1 + k] if k < i2 - i1 else ""
                t_txt = target_paras[j1 + k] if k < j2 - j1 else ""
                paras.append({"op": "replaced" if b_txt and t_txt else
                              ("removed" if b_txt else "added"),
                              "base": b_txt[:600], "target": t_txt[:600],
                              "hl": _char_hl(b_txt, t_txt)})
                numbers += _num_delta(b_txt, t_txt, section.get("title", ""))
        elif tag == "delete":
            for k in range(i1, i2):
                paras.append({"op": "removed", "base": base_paras[k][:600], "target": "",
                              "hl": []})
                numbers += _num_delta(base_paras[k], "", section.get("title", ""))
        else:
            for k in range(j1, j2):
                paras.append({"op": "added", "base": "", "target": target_paras[k][:600],
                              "hl": []})
                numbers += _num_delta("", target_paras[k], section.get("title", ""))
    entry["paras"] = paras
    entry["numbers"] = numbers
    entry["changed"] = changed
    return entry


def _char_hl(a: str, b: str) -> list[dict]:
    """replaced 段落的字符级高亮区间 [{a:[s,e], b:[s,e], tag}]（前端着色用）。"""
    if not a or not b:
        return []
    sm = difflib.SequenceMatcher(None, a[:600], b[:600], autojunk=False)
    return [{"a": [i1, i2], "b": [j1, j2], "tag": tag}
            for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _num_delta(base_txt: str, target_txt: str, section: str) -> list[dict]:
    out = []
    base_nums = set(_NUM.findall(base_txt))
    for v in dict.fromkeys(_NUM.findall(target_txt)):
        if v not in base_nums:
            out.append({"direction": "added", "value": v, "section": section})
    target_nums = set(_NUM.findall(target_txt))
    for v in dict.fromkeys(_NUM.findall(base_txt)):
        if v not in target_nums:
            out.append({"direction": "removed", "value": v, "section": section})
    return out


# ---------- 4. LLM 摘要（不可用时抽取式兜底） ----------

_SUMMARY_PROMPT = (
    "下面是同一篇论文两个版本（旧版→新版）的结构化差异。请输出 JSON："
    '{"bullets":[{"type":"结构|内容|数值|图表|结论","text":"一句话要点"}]}，'
    "3~5 条，只总结最重要的修订差异，数值差异必须点名（如 训练步数 100K→300K）。\n\n差异：\n"
)


def summarize(entry_sections: list[dict], all_numbers: list[dict],
              stats: dict) -> dict:
    digest = _digest(entry_sections, all_numbers, stats)
    res = llm.chat_json_validated(
        [{"role": "user", "content": _SUMMARY_PROMPT + digest}],
        _SummarySchema, module="version_diff",
        audit_prompt="[version_diff] summary", prompt_version="diff-v1")
    if res:
        try:
            items = _SummarySchema(**res).bullets
            bullets = [{"type": b.type, "text": b.text} for b in items if b.text][:5]
            if bullets:
                return {"bullets": bullets, "engine": "llm"}
        except Exception as e:  # noqa: BLE001 —— 校验失败走抽取式兜底
            logger.warning(f"diff LLM 摘要解析失败，走抽取式兜底: {type(e).__name__}", module="diff")
    return {"bullets": _extractive_summary(entry_sections, all_numbers, stats),
            "engine": "extractive"}


class _Bullet(BaseModel):
    type: str = "内容"
    text: str = ""


class _SummarySchema(BaseModel):
    bullets: list[_Bullet] = Field(default_factory=list)


def _digest(entry_sections: list[dict], all_numbers: list[dict], stats: dict) -> str:
    lines = [f"统计：{json.dumps(stats, ensure_ascii=False)}"]
    for s in entry_sections:
        if s["status"] != "aligned":
            lines.append(f"章节[{s['status']}]：{s['title']}")
        elif s.get("changed"):
            head = (s.get("paras") or [{}])[0]
            lines.append(f"章节[改]「{s['title']}」变化 {s['changed']} 处，"
                         f"例如 旧:{(head.get('base') or '')[:80]} → 新:{(head.get('target') or '')[:80]}")
    if all_numbers:
        lines.append("数值变化（前 20）："
                     + "；".join(f"{n['value']}({n['direction']} @{n['section'][:24]})"
                                 for n in all_numbers[:20]))
    return "\n".join(lines)[:4000]


def _extractive_summary(entry_sections: list[dict], all_numbers: list[dict],
                        stats: dict) -> list[dict]:
    bullets: list[dict] = []
    added = [s["title"] for s in entry_sections if s["status"] == "added"]
    removed = [s["title"] for s in entry_sections if s["status"] == "removed"]
    if added:
        bullets.append({"type": "结构", "text": f"新增章节：{('、'.join(added[:5]))}"})
    if removed:
        bullets.append({"type": "结构", "text": f"移除章节：{('、'.join(removed[:5]))}"})
    if all_numbers:
        pairs = [n for n in all_numbers if n["direction"] == "added"][:3]
        if pairs:
            bullets.append({"type": "数值",
                            "text": "数值变化：" + "；".join(
                                f"{n['value']}（{n['section'][:30]}）" for n in pairs)})
    changed = [s for s in entry_sections if s["status"] == "aligned" and s.get("changed")]
    if changed:
        bullets.append({"type": "内容", "text":
                        f"{len(changed)} 个章节有内容修改（最多：{changed[0]['title']}，"
                        f"{changed[0]['changed']} 处变更）"})
    if not bullets:
        bullets.append({"type": "结论", "text": "两版本结构高度一致，未检测到实质性修订差异"})
    return bullets[:5]


# ---------- 编排 ----------

def compute_diff(base_docir: dict, target_docir: dict) -> dict:
    """两份 DocIR → DiffLens 完整载荷（不落库，调用方 save_version_diff）。"""
    aligned = align_sections(base_docir.get("toc", []), target_docir.get("toc", []))
    sections = [para_diff(base_docir, target_docir, s) for s in aligned]
    all_numbers: list[dict] = []
    for s in sections:
        all_numbers += s.get("numbers", [])

    stats = {
        "sections_added": sum(1 for s in sections if s["status"] == "added"),
        "sections_removed": sum(1 for s in sections if s["status"] == "removed"),
        "sections_changed": sum(1 for s in sections
                                if s["status"] == "aligned" and s.get("changed")),
        "paras_changed": sum(s.get("changed", 0) for s in sections),
        "numbers_changed": len(all_numbers),
    }
    summary = summarize(sections, all_numbers, stats)

    # Top-N 限流：变化章节按 changed 降序保留明细；无变化章节只留 status 元信息
    ranked = sorted(sections, key=lambda s: (-s.get("changed", 0), s["status"] == "aligned"))
    keep = [s for s in ranked if s.get("changed") or s["status"] != "aligned"][:TOP_SECTIONS]
    keep_ids = {id(s) for s in keep}
    slim = []
    for s in sections:
        if id(s) in keep_ids:
            slim.append(s)
        else:
            slim.append({k: s[k] for k in ("status", "title", "base_title", "base_page",
                                           "target_page")})
            slim[-1]["detail_omitted"] = True
    return {"sections": slim, "numbers": all_numbers[:200], "summary": summary,
            "stats": stats}
