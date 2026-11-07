"""应答原子能力（从原 qa.py 迁移，供 responder 节点复用）。

七道防线在此落地：强制引用生成 → 引用后验校验 → ClaimTrace 逐论断 →
查无依据拒答 → 无 LLM 抽取式降级。
"""
from __future__ import annotations

import re
from typing import Any

from ..llm import client as llm

CIT = re.compile(r"\[\^p(\d+)(?:-§([^\]]*))?\]")

SYSTEM_QA = (
    "你是一名严谨的科研文献问答助手。回答规则：\n"
    "1. 只允许使用 <context> 中提供的原文片段作答，禁止使用任何外部知识。\n"
    "2. 每个事实性论断后必须紧跟引用标记，格式：[^p{页码}-§{章节号}]，页码与章节取自片段标签。\n"
    "3. 若 <context> 不足以回答，必须整体回答：「【未找到依据】检索到的片段无法回答该问题。」并给出建议（换关键词/指定论文）。\n"
    "4. 不确定的内容宁可不写；禁止编造数字、结论与引用。"
)

SYSTEM_CLAIMS = (
    "你是论断拆解器。将答案拆成原子论断数组，每个论断标注其在原文中的支撑片段。\n"
    "输出 JSON：{\"claims\":[{\"text\":\"论断原文\",\"status\":\"supported|unsupported\","
    "\"pages\":[引用页码数组]}]}。仅依据答案与片段标签判断，status=unsupported 表示该句无对应页码引用。"
)

REFUSAL_CHITCHAT = (
    "我是 Meridian 文献问答助手，只回答与库内论文相关的问题。"
    "请先在文献库上传 PDF，然后就该论文内容向我提问。"
)

NO_EVIDENCE = "【未找到依据】检索到的片段无法回答该问题。建议：换关键词重问，或确认已选中相关论文。"


def format_context(hits: list[dict]) -> tuple[str, dict[int, dict]]:
    """hits → 带标签上下文；返回 (文本, 页码→最佳chunk 映射用于后验)。"""
    parts: list[str] = []
    page_map: dict[int, dict] = {}
    for h in hits:
        sec = h.get("section_path") or "前言"
        parts.append(f"[片段 · p{h['page']} · §{sec}]\n{h['text'][:1200]}")
        page_map.setdefault(h["page"], h)
    return "\n\n".join(parts), page_map


def validate_citations(answer: str, page_map: dict[int, dict]) -> tuple[str, list[str]]:
    """引用后验校验：每个 [^pN] 的 N 必须在本次检索结果的页码集合中。"""
    guardrails: list[str] = []
    pages = set(page_map)

    def check(m: re.Match) -> str:
        p = int(m.group(1))
        if p in pages:
            return m.group(0)
        guardrails.append(f"invalid_citation:p{p}")
        return ""

    cleaned = CIT.sub(check, answer)
    if not CIT.search(answer):
        guardrails.append("no_citation_found")
    return cleaned, sorted(set(guardrails))


def extractive_answer(question: str, hits: list[dict]) -> tuple[str, list[dict]]:
    """无 LLM 降级：句子级关键词匹配摘句，逐句挂原文锚点。诚实标注模式。"""
    q_terms = [t.lower() for t in re.split(r"[^\w]+", question) if len(t) > 1][:12]
    if not q_terms:
        q_terms = [question.lower()[:20]]

    scored: list[tuple[float, str, dict]] = []
    for h in hits:
        for sent in re.split(r"(?<=[.!?。！？])\s+", h["text"]):
            sent = sent.strip()
            if len(sent) < 25 or len(sent) > 400:
                continue
            s_low = sent.lower()
            overlap = sum(1 for t in set(q_terms) if t in s_low)
            if overlap:
                scored.append((overlap / len(set(q_terms)), sent, h))
    scored.sort(key=lambda x: -x[0])

    claims: list[dict] = []
    answer_parts: list[str] = []
    used: set[str] = set()
    for score, sent, h in scored[:5]:
        key = sent[:80]
        if key in used:
            continue
        used.add(key)
        sec = h.get("section_path") or "前言"
        answer_parts.append(f"{sent} [^p{h['page']}-§{sec}]")
        claims.append({"text": sent, "status": "supported",
                       "support": [{"page": h["page"], "section_path": h["section_path"],
                                    "chunk_id": h["chunk_id"], "quote": sent[:160],
                                    "match_score": round(score, 3)}]})
    if not answer_parts:
        return NO_EVIDENCE, []
    note = ("\n\n> ⚠️ 当前处于**抽取式证据模式**（未配置 LLM 或调用失败）：以上句子为检索原文摘录，"
            "非模型生成综述。配置 LLM key 后自动升级为生成式溯源问答。")
    return "\n\n".join(answer_parts) + note, claims


def llm_claims(answer: str, hits: list[dict]) -> list[dict]:
    """ClaimTrace：一次结构化调用把答案拆成原子论断。"""
    context, _ = format_context(hits)
    res = llm.chat(
        [{"role": "system", "content": SYSTEM_CLAIMS},
         {"role": "user", "content": f"<answer>\n{answer}\n</answer>\n<context>\n{context[:6000]}\n</context>"}],
        json_mode=True, module="claims", audit_prompt=answer[:200],
    )
    data = llm.parse_json(res.get("text", ""))
    if not data or "claims" not in data:
        return []
    page_map = {h["page"]: h for h in hits}
    out: list[dict] = []
    for c in data["claims"]:
        if not isinstance(c, dict) or not c.get("text"):
            continue
        pages = [p for p in c.get("pages", []) if isinstance(p, (int, float))]
        support = []
        for p in pages:
            h = page_map.get(int(p))
            if h:
                support.append({"page": h["page"], "section_path": h["section_path"],
                                "chunk_id": h["chunk_id"], "quote": ""})
        out.append({"text": c["text"],
                    "status": "supported" if support else "unsupported",
                    "support": support})
    return out


def build_qa_messages(question: str, hits: list[dict]) -> list[dict]:
    context, _ = format_context(hits)
    return [{"role": "system", "content": SYSTEM_QA},
            {"role": "user", "content": f"<context>\n{context}\n</context>\n\n问题：{question}"}]


def hit_summary(hits: list[dict], limit: int = 8) -> list[dict]:
    """SSE retrieval 事件的 hits 载荷（协议与旧前端兼容）。"""
    return [{k: h.get(k) for k in ("chunk_id", "paper_id", "page", "section_path",
                                   "score", "rrf", "kind", "aspect")} | {"text": h.get("text", "")[:300]}
            for h in hits[:limit]]
