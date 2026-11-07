"""抗幻觉 RAG 问答（模块二 + 抓手1 ClaimTrace）。

管线：混合检索 top-20 → RRF → top-8 → 强制引用生成 `[^p{page}-§{section}]`
→ 引用后验校验（锚点必须存在于本次检索结果）→ 逐论断溯源拆解。
无 LLM 时自动降级「抽取式证据模式」：直接摘句作答并挂原文锚点。
"""
from __future__ import annotations

import json
import re
from typing import Any, Generator

from .. import db
from ..config import settings
from ..llm import client as llm
from . import indexing

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


def _format_context(hits: list[dict]) -> tuple[str, dict[str, dict]]:
    """hits → 带标签上下文；返回 (文本, 页码→最佳chunk 映射用于后验)。"""
    parts, page_map = [], {}
    for i, h in enumerate(hits, 1):
        sec = h.get("section_path") or "前言"
        parts.append(f"[片段{i} · p{h['page']} · §{sec}]\n{h['text'][:1200]}")
        page_map.setdefault(h["page"], h)
    return "\n\n".join(parts), page_map


def _validate_citations(answer: str, page_map: dict[int, dict]) -> tuple[str, list[str]]:
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


def _extractive_answer(question: str, hits: list[dict]) -> tuple[str, list[dict]]:
    """无 LLM 降级：句子级关键词匹配摘句，逐句挂原文锚点。诚实标注模式。"""
    q_terms = [t.lower() for t in re.split(r"[^\w]+", question + " ".join(
        "".join(ch if ch.isalnum() else " " for ch in question).split())) if len(t) > 1][:12]

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

    claims, answer_parts, used = [], [], set()
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
        return "【未找到依据】检索片段与问题无充分重叠，请换关键词或缩小范围。", []
    note = ("\n\n> ⚠️ 当前处于**抽取式证据模式**（未配置 LLM 或调用失败）：以上句子为检索原文摘录，"
            "非模型生成综述。配置 LLM key 后自动升级为生成式溯源问答。")
    return "\n\n".join(answer_parts) + note, claims


def _llm_claims(answer: str, hits: list[dict]) -> list[dict]:
    """ClaimTrace：一次结构化调用把答案拆成原子论断（抓手 1）。"""
    context, _ = _format_context(hits)
    res = llm.chat(
        [{"role": "system", "content": SYSTEM_CLAIMS},
         {"role": "user", "content": f"<answer>\n{answer}\n</answer>\n<context>\n{context[:6000]}\n</context>"}],
        json_mode=True, module="claims", audit_prompt=answer[:200],
    )
    data = llm.parse_json(res.get("text", ""))
    if not data or "claims" not in data:
        return []
    page_map_idx = {h["page"]: h for h in hits}
    out = []
    for c in data["claims"]:
        if not isinstance(c, dict) or not c.get("text"):
            continue
        pages = [p for p in c.get("pages", []) if isinstance(p, (int, float))]
        support = []
        for p in pages:
            h = page_map_idx.get(int(p))
            if h:
                support.append({"page": h["page"], "section_path": h["section_path"],
                                "chunk_id": h["chunk_id"], "quote": ""})
        out.append({"text": c["text"],
                    "status": "supported" if support else "unsupported",
                    "support": support})
    return out


def answer_stream(question: str, paper_ids: list[str]) -> Generator[dict, None, None]:
    """SSE 事件流。事件：retrieval → delta* → final → done。"""
    # 1) 混合检索
    search = indexing.hybrid_search(paper_ids, question, top_k=20)
    hits = search["hits"][:8]
    yield {"event": "retrieval", "data": {
        "channels": search["channels"], "bm25_count": search["bm25_count"],
        "dense_count": search["dense_count"],
        "hits": [{k: h.get(k) for k in ("chunk_id", "paper_id", "page", "section_path",
                                        "score", "rrf", "kind")} | {"text": h["text"][:300]}
                 for h in hits]}}
    if not hits:
        yield {"event": "final", "data": {"answer": "【未找到依据】文献库为空或未命中，请先上传并完成解析。",
                                          "claims": [], "mode": "none", "guardrails": ["empty_index"]}}
        yield {"event": "done", "data": {}}
        return

    context, page_map = _format_context(hits)
    guardrails: list[str] = []

    # 2) 生成式（LLM 可用） / 抽取式降级
    if llm.available():
        messages = [{"role": "system", "content": SYSTEM_QA},
                    {"role": "user", "content": f"<context>\n{context}\n</context>\n\n问题：{question}"}]
        answer_parts: list[str] = []
        stream_ok = False
        for piece in llm.stream_chat(messages, max_tokens=1500):
            stream_ok = True
            answer_parts.append(piece)
            yield {"event": "delta", "data": {"text": piece}}
        answer = "".join(answer_parts)
        if not stream_ok or not answer.strip():
            # 流式失败 → 同步重试一次
            res = llm.chat(messages, max_tokens=1500, module="qa", audit_prompt=question)
            answer = res.get("text", "")
            if answer:
                yield {"event": "delta", "data": {"text": answer}}
        if not answer.strip():
            guardrails.append("llm_unavailable_fallback")
            answer, claims = _extractive_answer(question, hits)
            yield {"event": "final", "data": {"answer": answer, "claims": claims,
                                              "mode": "extractive", "guardrails": guardrails}}
            yield {"event": "done", "data": {}}
            return

        # 3) 引用后验校验
        answer, cited_guards = _validate_citations(answer, page_map)
        guardrails += cited_guards
        if "no_citation_found" in guardrails:
            # 重试一次：更严格的提醒
            retry = llm.chat(messages + [{"role": "assistant", "content": answer},
                                         {"role": "user", "content":
                                          "你的回答缺少引用标记。请严格按 [^p{页码}-§{章节}] 格式重写，"
                                          "只改引用不改内容。"}], max_tokens=1500, module="qa_retry")
            if retry.get("text"):
                answer, g2 = _validate_citations(retry["text"], page_map)
                guardrails += g2
        if "no_citation_found" in guardrails:
            answer += "\n\n> ⚠️ 该回答未通过引用后验校验（无有效页码锚点），请人工核对原文。"

        # 4) ClaimTrace 逐论断拆解
        claims = _llm_claims(answer, hits)
        yield {"event": "final", "data": {"answer": answer, "claims": claims,
                                          "mode": "generative", "guardrails": guardrails}}
    else:
        answer, claims = _extractive_answer(question, hits)
        yield {"event": "final", "data": {"answer": answer, "claims": claims,
                                          "mode": "extractive", "guardrails": ["llm_unavailable_fallback"]}}
    db.log_qa(module="qa", question=question, answer=answer[:8000],
              guardrail=";".join(guardrails) or "ok")
    yield {"event": "done", "data": {"temperature": settings.llm_temperature}}
