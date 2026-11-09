"""应答原子能力（供 responder 节点复用）。

四层防御在生成侧的落地：Evidence Pack 显式编号（E1..En）→ 强制 [E#] 引用生成 →
引用后验校验（L1 存在性）→ ClaimTrace 逐论断（L2/L3/L4 校验见 rag/verify.py）→
查无依据拒答（附最接近证据）→ 无 LLM 抽取式降级。

引用标记：[E3] —— 编号对应 format_context 打包的证据片段，校验/溯源精确到块级
（page/bbox/section_path 随 eid_map 可回查）。SSE 对外页码统一 1 基（人读页码）。
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..llm import client as llm
from ..llm.prompts import resolve as _resolve
from ..llm.schemas import ClaimList

CIT = re.compile(r"\[E(\d+)\]")

SYSTEM_QA = (
    "你是一名严谨的科研文献问答助手。回答规则：\n"
    "1. 只允许使用 <context> 中提供的证据片段作答，禁止使用任何外部知识。\n"
    "2. 每个事实性论断后必须紧跟引用标记，格式：[E编号]，编号取自证据片段的方括号标签"
    "（如 [E1]、[E3]），禁止引用 <context> 中不存在的编号。\n"
    "3. 若 <context> 不足以回答，必须整体回答：「【未找到依据】检索到的片段无法回答该问题。」"
    "并给出建议（换关键词/指定论文）。\n"
    "4. 不确定的内容宁可不写；禁止编造数字、结论与引用；数值必须逐字来自证据原文。"
)

SYSTEM_CLAIMS = (
    "你是论断拆解器。将答案拆成原子论断数组，每个论断标注其支撑证据与原文引句。\n"
    "输出 JSON：{\"claims\":[{\"text\":\"论断原文\",\"evidence_ids\":[支撑证据编号数组],"
    "\"quote\":\"论断依据的原文片段，逐字摘自对应证据文本，不超过200字\"}]}。"
    "仅依据答案与证据标签判断；证据编号必须来自 <context> 标签，"
    "论断在证据中无出处时 evidence_ids 给空数组。"
)

REFUSAL_CHITCHAT = (
    "我是 Meridian 文献问答助手，只回答与库内论文相关的问题。"
    "请先在文献库上传 PDF，然后就该论文内容向我提问。"
)

NO_EVIDENCE = "【未找到依据】检索到的片段无法回答该问题。建议：换关键词重问，或确认已选中相关论文。"

COMPARE_REFUSAL = (
    "【Compare Agent · 范围校验】跨文对比需要选中 **2 篇及以上** 论文，当前只有 {n} 篇。\n\n"
    "建议：\n"
    "- 在文献库中再勾选一篇论文后重新提问；\n"
    "- 或改用 **Reader Agent（单篇精读）** 就当前论文提问。\n"
    "> 我不会用单篇内容冒充对比结论。"
)

SYSTEM_QA_COMPARE = (
    "你是一名严谨的跨论文对比分析助手。回答规则：\n"
    "1. 只允许使用 <context> 中的证据片段与 <compare_table> 对齐表作答，禁止使用任何外部知识。\n"
    "2. 按维度组织回答：每个维度一行/一段，行内明确标注结论来自哪篇论文"
    "（用证据标签中的论文代号 P1/P2 或标题）。\n"
    "3. 每个事实性论断后必须紧跟引用标记 [E编号]，编号取自证据片段标签，"
    "禁止引用 <context> 中不存在的编号。\n"
    "4. 两个论文在同一维度结论冲突时，必须双侧并列呈现，禁止擅自合并或替用户裁决。\n"
    "5. 对齐表中某篇某维度标记「未找到」时，如实写明该篇未检索到相关内容，禁止脑补。\n"
    "6. 不确定的内容宁可不写；禁止编造数字、结论与引用；数值必须逐字来自证据原文。"
)

WRITER_MIN_EVIDENCE = 3

WRITER_REFUSAL = (
    "【Writer Agent · 证据池校验】当前证据池只有 {n} 块（要求 ≥{m} 块），不足支撑一篇有据可查的写作。\n\n"
    "我不会在无证据的情况下凭空生成文稿。建议：\n"
    "- 换更具体的问题让检索命中更多相关段落；\n"
    "- 或先用 **Reader Agent（单篇精读）** 确认文献里有相关内容。\n"
    "> Evidence-First：没有证据就没有写作。"
)

SYSTEM_QA_WRITER = (
    "你是一名严谨的学术写作助手。根据给定的证据池写一篇带引用的短文（≤600字）。规则：\n"
    "1. 只允许使用 <context> 中的证据片段作答，禁止使用任何外部知识。\n"
    "2. 严格按 <outline> 的大纲结构分节输出（小节标题加粗），不要增删小节。\n"
    "3. 每个事实性论断后必须紧跟引用标记 [E编号]，编号取自证据片段标签，"
    "禁止引用 <context> 中不存在的编号。\n"
    "4. 证据不足以支撑的小节，如实写「（本节证据不足，仅列出待补要点）」，禁止脑补。\n"
    "5. 语言学术、克制，不使用感叹号；禁止编造数字、结论与引用；数值必须逐字来自证据原文。"
)

SYSTEM_QA_GRAPH = (
    "你是引用关系分析助手。依据 <graph_metrics>（由 references 元数据真实计算，含 PageRank/度）"
    "与 <context> 中的证据片段，写一段引用关系综述（≤300字）。规则：\n"
    "1. 指标数字只允许来自 <graph_metrics>，禁止编造或四舍五入夸大。\n"
    "2. 原文内容性论断必须带 [E编号] 引用标记（编号取自证据片段标签）。\n"
    "3. 图中边很少时如实说明（如「库内仅 X 篇论文，引用边 Y 条，指标仅供参考」），禁止夸大图谱结论。\n"
    "4. external_count>0 时说明存在未能匹配到库内论文的外部引用。"
)

# ---- Prompt 版本化（app/prompts/*.yaml 优先，缺失/非法回退上方内置常量；4.5）----
# system_* 在 build_*_messages 内运行时 resolve（支持 PROMPT_VARIANT 运行时 A/B）；
# 拒答文案为静态拷贝，模块加载时 resolve 一次。
REFUSAL_CHITCHAT, _ = _resolve("qa", "refusal_chitchat", REFUSAL_CHITCHAT)
NO_EVIDENCE, _ = _resolve("qa", "no_evidence", NO_EVIDENCE)
COMPARE_REFUSAL, _ = _resolve("compare", "compare_refusal", COMPARE_REFUSAL)
WRITER_REFUSAL, _ = _resolve("writer", "writer_refusal", WRITER_REFUSAL)


def build_writer_messages(question: str, outline: dict, hits: list[dict],
                          language: str = "") -> list[dict]:
    """Writer Agent 专属消息：大纲 + 证据池。"""
    context, _ = format_context(hits)
    outline_txt = json.dumps(outline, ensure_ascii=False)
    system, _ = _resolve("writer", "system_writer", SYSTEM_QA_WRITER)
    return [{"role": "system", "content": _with_language(system, language)},
            {"role": "user", "content":
                f"<outline>\n{outline_txt}\n</outline>\n\n<context>\n{context}\n</context>"
                f"\n\n写作请求：{question}"}]


def build_graph_messages(metrics: dict, question: str, hits: list[dict],
                         language: str = "") -> list[dict]:
    """Graph Agent 专属消息：真实计算的图谱指标 + 证据池。"""
    context, _ = format_context(hits) if hits else ("（无补充证据片段）", {})
    metrics_txt = json.dumps(metrics, ensure_ascii=False)
    system, _ = _resolve("graph", "system_graph", SYSTEM_QA_GRAPH)
    return [{"role": "system", "content": _with_language(system, language)},
            {"role": "user", "content":
                f"<graph_metrics>\n{metrics_txt}\n</graph_metrics>\n\n<context>\n{context}\n</context>"
                f"\n\n问题：{question}"}]


def build_compare_messages(question: str, table: dict | None, hits: list[dict],
                           language: str = "") -> list[dict]:
    """Compare Agent 专属消息：上下文 + 维度对齐表。"""
    context, _ = format_context(hits)
    table_txt = json.dumps(table, ensure_ascii=False) if table else "（无对齐表）"
    system, _ = _resolve("compare", "system_compare", SYSTEM_QA_COMPARE)
    return [{"role": "system", "content": _with_language(system, language)},
            {"role": "user", "content":
                f"<context>\n{context}\n</context>\n\n<compare_table>\n{table_txt}\n</compare_table>"
                f"\n\n对比问题：{question}"}]


def _with_language(system: str, language: str) -> str:
    """语言边界 guardrail：intent.language 指示用户语言 → 中文问中文答。"""
    return f"{system}\n语言边界：使用 {language or 'zh'} 回答。" if language else system


def build_paper_labels(hits: list[dict]) -> dict[str, str]:
    """多论文时给 paper_id 分配 P1/P2 代号（按首次出现顺序），单篇返回空映射。"""
    papers = list(dict.fromkeys(h.get("paper_id", "") for h in hits))
    if len(papers) <= 1:
        return {}
    return {pid: f"P{i + 1}" for i, pid in enumerate(papers)}


def format_context(hits: list[dict]) -> tuple[str, dict[int, dict]]:
    """hits → 显式编号 Evidence Pack（E1..En）；返回 (文本, eid→hit 映射用于后验与溯源)。"""
    labels = build_paper_labels(hits)
    parts: list[str] = []
    eid_map: dict[int, dict] = {}
    for i, h in enumerate(hits, 1):
        sec = h.get("section_path") or "前言"
        where = f"p{h['page'] + 1}"  # 对外 1 基人读页码
        tag = f"[E{i}] ({labels[h['paper_id']] + ' · ' if h.get('paper_id') in labels else ''}{where}, §{sec}) "
        parts.append(f"{tag}\n{h['text'][:1200]}")
        eid_map[i] = h
    return "\n\n".join(parts), eid_map


def build_anchors(eid_map: dict[int, dict]) -> dict[str, dict]:
    """eid_map → final.anchors：E# → 块级溯源锚点（页码 1 基，bbox 归一化）。"""
    labels = build_paper_labels(list(eid_map.values()))
    out: dict[str, dict] = {}
    for eid, h in eid_map.items():
        out[f"E{eid}"] = {
            "page": h["page"] + 1,  # 1 基
            "section_path": h.get("section_path") or "前言",
            "paper_id": h.get("paper_id", ""),
            "paper_label": labels.get(h.get("paper_id", ""), ""),
            "bbox": h.get("bbox") or [],
            "chunk_id": h.get("chunk_id", ""),
            "block_ids": h.get("block_ids") or [],
        }
    return out


def validate_citations(answer: str, eid_map: dict[int, dict]) -> tuple[str, list[str]]:
    """引用后验校验（L1 存在性）：每个 [E#] 的编号必须在本次证据包内，无效锚点剔除。"""
    guardrails: list[str] = []
    valid = set(eid_map)

    def check(m: re.Match) -> str:
        n = int(m.group(1))
        if n in valid:
            return m.group(0)
        guardrails.append(f"invalid_citation:E{n}")
        return ""

    cleaned = CIT.sub(check, answer)
    if not CIT.search(answer):
        guardrails.append("no_citation_found")
    return cleaned, sorted(set(guardrails))


def near_evidence_block(hits: list[dict], k: int = 3) -> str:
    """拒答时附「最接近的 k 条证据」（设计文档拒答策略：供人工判断，而非硬编）。"""
    if not hits:
        return ""
    lines = ["", "", "> **最接近的证据**（未找到明确表述，以下供人工判断）："]
    for i, h in enumerate(hits[:k], 1):
        sec = h.get("section_path") or "前言"
        lines.append(f"> {i}. [E{i}] p{h['page'] + 1} · §{sec} · {h['text'][:140].strip()}…")
    return "\n".join(lines)


_EXTRACTIVE_STOP = {
    # 中英功能词：剔除后关键词重合才反映实义命中（否则无关英文问题也会"摘出"句子）
    "how", "what", "which", "who", "when", "where", "why", "do", "does", "did",
    "is", "are", "was", "were", "the", "an", "of", "on", "in", "at", "to",
    "for", "and", "or", "you", "your", "we", "it", "its", "this", "that",
    "with", "by", "from", "as", "be", "been", "can", "will", "would",
    "请", "一下", "什么", "怎么", "怎样", "哪些", "还是", "以及", "关于",
}


def extractive_answer(question: str, hits: list[dict]) -> tuple[str, list[dict]]:
    """无 LLM 降级：句子级关键词匹配摘句，逐句挂 [E#] 原文锚点。诚实标注模式。"""
    q_terms = [t.lower() for t in re.split(r"[^\w]+", question) if len(t) > 1][:20]
    q_terms = [t for t in q_terms if t not in _EXTRACTIVE_STOP][:12]
    if not q_terms:
        q_terms = [question.lower()[:20]]

    scored: list[tuple[float, str, dict, int]] = []
    for n, h in enumerate(hits, 1):
        for sent in re.split(r"(?<=[.!?。！？])\s+", h["text"]):
            sent = sent.strip()
            if len(sent) < 25 or len(sent) > 400:
                continue
            s_low = sent.lower()
            overlap = sum(1 for t in set(q_terms) if t in s_low)
            if overlap:
                scored.append((overlap / len(set(q_terms)), sent, h, n))
    scored.sort(key=lambda x: -x[0])

    claims: list[dict] = []
    answer_parts: list[str] = []
    used: set[str] = set()
    for score, sent, h, n in scored[:5]:
        key = sent[:80]
        if key in used:
            continue
        used.add(key)
        answer_parts.append(f"{sent} [E{n}]")
        claims.append({"text": sent, "status": "supported",
                       "support": [{"eid": f"E{n}", "page": h["page"] + 1,
                                    "section_path": h["section_path"],
                                    "chunk_id": h["chunk_id"], "quote": sent[:160],
                                    "match_score": round(score, 3)}]})
    if not answer_parts:
        return NO_EVIDENCE, []
    note = ("\n\n> ⚠️ 当前处于**抽取式证据模式**（未配置 LLM 或调用失败）：以上句子为检索原文摘录，"
            "非模型生成综述。配置 LLM key 后自动升级为生成式溯源问答。")
    return "\n\n".join(answer_parts) + note, claims


def _parse_eid(e: Any) -> int | None:
    """LLM 返回的 evidence_ids 兼容解析：1 / 1.0 / "1" / "E1" 都收。"""
    if isinstance(e, bool):
        return None
    if isinstance(e, int):
        return e
    if isinstance(e, float) and e.is_integer():
        return int(e)
    if isinstance(e, str):
        m = re.match(r"^E?(\d+)$", e.strip(), re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def llm_claims(answer: str, hits: list[dict]) -> tuple[list[dict], dict[int, dict]]:
    """ClaimTrace：一次结构化调用把答案拆成原子论断（经 Pydantic 校验 + 修复重试）。

    返回 (claims, eid_map)：claim.support 携带 eid + 块级锚点（页码 1 基），
    quote 交由 rag/verify.py 做 L2 子串校验（此处只透传 LLM 输出）。
    """
    context, eid_map = format_context(hits)
    system, pv = _resolve("claims", "system_claims", SYSTEM_CLAIMS)
    data = llm.chat_json_validated(
        [{"role": "system", "content": system},
         {"role": "user", "content": f"<answer>\n{answer}\n</answer>\n<context>\n{context[:6000]}\n</context>"}],
        ClaimList, retries=1, module="claims", audit_prompt=answer[:200], prompt_version=pv,
    )
    if not data:
        return [], eid_map
    out: list[dict] = []
    for c in data.get("claims", []):
        if not isinstance(c, dict) or not c.get("text"):
            continue
        # schema 已把 evidence_ids 宽松矫正为 int 列表；此处只按证据包过滤
        eids = [n for n in c.get("evidence_ids") or [] if n in eid_map]
        support = []
        for e in dict.fromkeys(eids):
            h = eid_map[e]
            support.append({"eid": f"E{e}", "page": h["page"] + 1,
                            "section_path": h["section_path"], "chunk_id": h["chunk_id"],
                            "quote": str(c.get("quote") or "")})
        out.append({"text": c["text"],
                    "status": "supported" if support else "unsupported",
                    "support": support})
    return out, eid_map


def build_qa_messages(question: str, hits: list[dict], language: str = "") -> list[dict]:
    context, _ = format_context(hits)
    system, _ = _resolve("qa", "system_qa", SYSTEM_QA)
    return [{"role": "system", "content": _with_language(system, language)},
            {"role": "user", "content": f"<context>\n{context}\n</context>\n\n问题：{question}"}]


def hit_summary(hits: list[dict], limit: int = 8) -> list[dict]:
    """SSE retrieval 事件的 hits 载荷（页码 1 基；协议与前端兼容）。"""
    keys = ("chunk_id", "paper_id", "section_path", "score", "rrf", "kind", "aspect", "rerank")
    out = []
    for h in hits[:limit]:
        item = {k: h.get(k) for k in keys} | {"text": h.get("text", "")[:300]}
        item["page"] = h.get("page", 0) + 1  # 0 基 → 1 基
        out.append(item)
    return out


# ---------- support 级原句块锚点（细粒度 P0：chunk 级 → 原句块级） ----------

def refine_support_blocks(claims: list[dict], evidence: list[dict]) -> int:
    """把每个 support 的 quote 定位回其引用 chunk 内的具体 DocIR 块（两轮）。

    - 第一轮：quote 归一化后完整包含于候选块文本 → 精确锚点（block_id/block_bbox）；
    - 第二轮：difflib 覆盖率最高块 ≥0.6 → 估算锚点（block_estimated=True，前端同款红框）；
    - claim 的 L2 quote_status=unverified（引文为转述/拼凑）→ **跳过定位**——假引文
      不给块级锚点（诚实回退 chunk 级 E# 锚点）；
    - quote 过短（<12 归一字符）不定位（噪声比信号大）。返回定位数。
    """
    from .verify import _norm as _norm_txt  # 局部导入：避免 rag 内环
    from .verify import quote_coverage
    from .version_diff import load_docir_for

    ev_by_chunk = {h.get("chunk_id"): h for h in evidence if h.get("chunk_id")}
    index_cache: dict[str, tuple[dict, dict[int, list[str]]]] = {}

    def _index(pid: str) -> tuple[dict, dict[int, list[str]]]:
        if pid not in index_cache:
            data = load_docir_for(pid) or {}
            by_id: dict[str, tuple[str, list]] = {}
            by_page: dict[int, list[str]] = {}
            for b in data.get("blocks", []):
                bid = b.get("id", "")
                if not bid:
                    continue
                by_id[bid] = (_norm_txt(b.get("text", "")), b.get("bbox") or [])
                by_page.setdefault(int(b.get("page", 0)), []).append(bid)
            index_cache[pid] = (by_id, by_page)
        return index_cache[pid]

    located = 0
    for c in claims:
        for s in c.get("support", []):
            if s.get("block_id"):
                continue   # 幂等：已定位跳过
            if c.get("quote_status") == "unverified":
                continue   # 假引文不给块锚点（诚实回退 chunk 级）
            qn = _norm_txt(s.get("quote", "") or "")
            chunk = ev_by_chunk.get(s.get("chunk_id"))
            if not chunk or len(qn) < 12:
                continue
            by_id, by_page = _index(chunk.get("paper_id", ""))
            cand_ids = list(chunk.get("block_ids") or []) \
                or by_page.get(int(chunk.get("page", 0) or 0), [])
            exact: tuple[str, list] | None = None
            best_bid, best_cov = None, 0.0
            for bid in cand_ids:
                nt, bbox = by_id.get(bid, ("", []))
                if not nt:
                    continue
                if qn in nt and exact is None:
                    exact = (bid, bbox)
                cov = quote_coverage(s.get("quote", ""), nt)
                if cov > best_cov:
                    best_bid, best_cov = bid, cov
            if exact is not None and len(exact[1]) == 4:
                s["block_id"], s["block_bbox"] = exact
                located += 1
            elif best_bid and best_cov >= 0.6 and len(by_id[best_bid][1]) == 4:
                s["block_id"] = best_bid
                s["block_bbox"] = by_id[best_bid][1]
                s["block_estimated"] = True   # 估算锚点：前端同款红框，口径诚实区分
                located += 1
    return located
