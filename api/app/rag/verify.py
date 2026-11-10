"""Claim-Citation 校验器（四层防御④，方案 §5.4 L1-L4）——「编了会被抓住」的核心。

分级流水（在 responder 的 ClaimTrace 之后执行）：
- L1 存在性：[E#] ∈ 证据包 —— rag/answering.validate_citations 已做（无效剔除 + guardrail）
- L2 引文子串：claim.quote 归一化后须与被引证据原文高度重合（覆盖率 ≥0.85）；
  失败逐 support 重抽×2 轮，仍失败标 quote_status=unverified（不占全局重写预算）
- L3 数值/实体：claim 中的数字/型号 token 必须逐字出现在被引证据文本中
  （纯规则、零 LLM 成本）；未命中 → status=needs_check（前端「需核对」黄标）
- L4 语义支持：批量一次 LLM 调用判每条 claim 能否被证据推出（yes/partial/no）；
  no → unsupported；调用/解析失败 → 规则兜底跳过（不误杀），guardrail=semantic_judge_skipped

claim 最终三态：supported（绿）/ needs_check（黄，数值或引文待人工核对）/ unsupported（未验证）。
"""
from __future__ import annotations

import difflib
import json
import re

from ..config import settings
from ..llm import client as llm
from ..llm.prompts import resolve as _resolve
from ..llm.schemas import QuoteMap, VerdictList
from ..log import logger

QUOTE_COVERAGE_MIN = 0.85

# 数值：整数/小数/百分号（千分位逗号先归一）；实体：全大写或含连字符的型号 token（P100/GPT-4/BLEU）
_NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")
_ENTITY_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Za-z0-9.]+)+\b|\b[A-Z]{2,}\d*\b")


def _norm(text: str) -> str:
    """归一化：全角数字→半角、去千分位逗号、去所有空白、小写。"""
    t = str(text or "")
    t = t.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    t = t.replace(",", "")
    return re.sub(r"\s+", "", t).lower()


def quote_coverage(quote: str, evidence_text: str) -> float:
    """quote 在证据文本中的覆盖率（归一化后最长匹配块覆盖 quote 的比例）。"""
    q, t = _norm(quote), _norm(evidence_text)
    if not q or not t:
        return 0.0
    sm = difflib.SequenceMatcher(None, q, t, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    return matched / len(q)


def extract_claims_numbers(claim_text: str) -> list[str]:
    """抽取 claim 中须逐字对回证据的数值/实体 token（归一化形式）。"""
    t = str(claim_text or "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    t = t.replace(",", "")
    found = _NUM_RE.findall(t) + _ENTITY_RE.findall(t)
    return list(dict.fromkeys(_norm(f) for f in found if _norm(f)))


# ---------- L2 引文子串 ----------

def _support_evidence_text(s: dict, eid_map: dict[int, dict]) -> str:
    """单个 support 引用的证据文本（按 s.eid 精确对位——quote 必须来自它引用的那条）。"""
    eid = str(s.get("eid", ""))
    idx = int(eid[1:]) if eid[1:].isdigit() else None
    if idx is None or idx not in eid_map:
        return ""
    return eid_map[idx].get("text", "")


def check_quotes(claims: list[dict], eid_map: dict[int, dict]) -> tuple[list[dict], list[str]]:
    """L2（P0 修订 + Round5 加强）：逐 support 校验——每个 support 的 quote 对**它自己
    引用的证据**算覆盖率，全部 ≥0.85 才 verified（旧实现只校验 support[0]）。

    Round5 修复两处真实缺陷（round4 实测 quote_unverified 0.17 压线超门禁的来源）：
    (1) 旧重抽只回填**覆盖率最低的一条** support——多支持 claim 其余不合格引文留存，
        min 覆盖率仍 <0.85，重抽做了等于没做；
    (2) 只跑 1 轮。现改为**逐 support 重抽**（键=论断|E#）×2 轮，替换仅在变好时生效。
    """
    guardrails: list[str] = []
    for c in claims:
        if not c.get("support"):
            continue  # 无出处论断（unsupported）不做 L2 quote 校验
        covs = [round(quote_coverage(s.get("quote", ""), _support_evidence_text(s, eid_map)), 3)
                for s in c["support"]]
        c["quote_coverages"] = covs
        min_cov = min(covs) if covs else 0.0
        c["quote_status"] = "verified" if covs and min_cov >= QUOTE_COVERAGE_MIN else "unverified"
        c["quote_coverage"] = round(min_cov, 3)

    for _ in range(2):   # Round5：重抽 2 轮（第 2 轮只处理残余，通常为空批）
        failed = [c for c in claims
                  if c.get("support") and c.get("quote_status") == "unverified"]
        if not failed or not llm.available():
            break
        re_quotes = _re_extract_quotes(failed, eid_map)
        applied = False
        for c in failed:
            covs = c.get("quote_coverages") or []
            for k, s in enumerate(c["support"]):
                key = f"{c['text']}|{s.get('eid', '')}"
                new_q = re_quotes.get(key) or re_quotes.get(c["text"])   # 兼容旧单键
                if not new_q:
                    continue
                cov = round(quote_coverage(new_q, _support_evidence_text(s, eid_map)), 3)
                if cov > (covs[k] if k < len(covs) else 0.0):   # 只在变好时替换
                    s["quote"] = new_q
                    if k < len(covs):
                        covs[k] = cov
                    applied = True
            min_cov = min(covs) if covs else 0.0
            c["quote_status"] = "verified" if covs and min_cov >= QUOTE_COVERAGE_MIN else "unverified"
            c["quote_coverage"] = round(min_cov, 3)
        if not applied:
            break

    n_bad = sum(1 for c in claims if c.get("quote_status") == "unverified")
    if n_bad:
        guardrails.append(f"quote_unverified:{n_bad}")
    return claims, guardrails


def _evidence_texts(claim: dict, eid_map: dict[int, dict]) -> list[str]:
    idxs = [int(s["eid"][1:]) for s in claim.get("support", [])
            if str(s.get("eid", ""))[1:].isdigit() and int(str(s["eid"])[1:]) in eid_map]
    return [eid_map[i]["text"] for i in idxs]


_BUILTIN_REEXTRACT = (
    "你是引文抽取器。为每条「论断|引用」组合从其引用的证据片段中逐字摘录支撑该论断的原文片段"
    "（空白与原文完全一致，禁止改写、概括或拼凑）。"
    '输出 JSON：{"quotes":{"论断原文|E编号":"摘录的原文片段",...}}，键必须与输入论断一一对应。'
)


def _re_extract_quotes(failed: list[dict], eid_map: dict[int, dict]) -> dict[str, str]:
    """L2 重抽：一次批量调用为未通过 quote 校验的论断重新逐字摘录原文片段。"""
    context, _eid_map = _pack_evidence(eid_map)
    lines: list[str] = []
    for c in failed:
        covs = c.get("quote_coverages") or []
        for k, s in enumerate(c["support"]):
            if k < len(covs) and covs[k] >= QUOTE_COVERAGE_MIN:
                continue   # 已合格的 support 不重抽（省 token，也避免把好的换坏）
            lines.append(f"- 论断：{c['text']}\n  引用：{s.get('eid', '')}")
    system, pv = _resolve("claims", "quote_reextract", _BUILTIN_REEXTRACT)
    data = llm.chat_json_validated(
        [{"role": "system", "content": system},
         {"role": "user", "content": f"<claims>\n{chr(10).join(lines)}\n</claims>\n\n<evidence>\n{context[:6000]}\n</evidence>"}],
        QuoteMap, retries=1, module="quote_reextract",
        audit_prompt=failed[0]["text"][:200] if failed else "",
        prompt_version=pv, temperature=0.0,
    )
    return (data or {}).get("quotes") or {}


def _pack_evidence(eid_map: dict[int, dict]) -> tuple[str, dict[int, dict]]:
    parts = [f"[E{i}] (p{h['page'] + 1}, §{h.get('section_path') or '前言'})\n{h['text'][:1200]}"
             for i, h in sorted(eid_map.items())]
    return "\n\n".join(parts), eid_map


# ---------- L3 数值/实体 ----------

def check_numbers(claims: list[dict], eid_map: dict[int, dict]) -> tuple[list[dict], list[str]]:
    """claim 中的数值/型号必须逐字出现在被引证据文本中；未命中 → needs_check 黄标。"""
    guardrails: list[str] = []
    n_bad = 0
    for c in claims:
        if c.get("status") != "supported" or not c.get("support"):
            continue
        tokens = extract_claims_numbers(c["text"])
        if not tokens:
            continue
        haystack = _norm(" ".join(_evidence_texts(c, eid_map)))
        missing = [t for t in tokens if t not in haystack]
        if missing:
            c["status"] = "needs_check"
            c["unverified_numbers"] = missing
            n_bad += 1
    if n_bad:
        guardrails.append(f"number_not_in_evidence:{n_bad}")
    return claims, guardrails


# ---------- L4 语义支持 ----------

_SYSTEM_JUDGE = (
    "你是语义支持判定器。对每条论断判断它能否由其引用的证据文本推出（不要求逐字一致，"
    "语义一致即可；证据不能推出或相矛盾时判 no）。"
    '输出 JSON：{"verdicts":[{"id":1,"verdict":"yes|partial|no"}]}，id 与输入编号一一对应，'
    "不要输出其他字段。"
)


def _mark_unchecked(claims: list[dict], cand: list[tuple[int, dict]]) -> None:
    """L4 缺席的诚实标注：绿色「已验证」仅代表 L2/L3 通过，语义判定未执行。"""
    for _i, c in cand:
        c["semantic_status"] = "unchecked"


def judge_claims(claims: list[dict], eid_map: dict[int, dict]) -> tuple[list[dict], list[str]]:
    """L4：批量一次调用判定每条 claim 的语义支持性。verdict=no → unsupported。
    开关关闭 / LLM 不可用 / 判定失败 → 候选 claim 一律标 semantic_status=unchecked
    （P0 修订：旧实现静默返回，claims 伪装绿色「已验证」且无任何 guardrail）。"""
    guardrails: list[str] = []
    cand = [(i, c) for i, c in enumerate(claims)
            if c.get("status") in ("supported", "needs_check") and c.get("support")]
    if not cand:
        return claims, guardrails
    if not settings.semantic_judge:
        _mark_unchecked(claims, cand)
        guardrails.append("semantic_judge_off")
        return claims, guardrails
    if not llm.available():
        _mark_unchecked(claims, cand)
        guardrails.append("semantic_judge_skipped")
        return claims, guardrails

    context, _ = _pack_evidence(eid_map)
    lines = []
    for i, c in cand:
        ev = "；".join(f"[{s.get('eid', '')}]" for s in c["support"])
        lines.append(f'{{"id":{i},"claim":{json.dumps(c["text"], ensure_ascii=False)},"cited":{ev}}}')
    try:
        system, pv = _resolve("judge", "system_judge", _SYSTEM_JUDGE)
        data = llm.chat_json_validated(
            [{"role": "system", "content": system},
             {"role": "user", "content":
                 f"<claims>\n[{', '.join(lines)}]\n</claims>\n\n<evidence>\n{context[:6000]}\n</evidence>"}],
            VerdictList, retries=1, module="judge",
            audit_prompt=claims[cand[0][0]]["text"][:200],
            prompt_version=pv, temperature=0.0,
        )
        verdicts = data.get("verdicts") if isinstance(data, dict) else None
        if not isinstance(verdicts, list):
            raise ValueError("judge payload malformed")  # noqa: TRY004
        vmap = {v.get("id"): str(v.get("verdict", "")).lower()
                for v in verdicts if isinstance(v, dict)}
    except Exception as e:  # noqa: BLE001 —— L4 属增强校验，失败规则兜底跳过（不误杀）
        logger.warning(f"semantic judge 跳过（规则兜底）: {type(e).__name__}: {e}", module="verify")
        _mark_unchecked(claims, cand)
        guardrails.append("semantic_judge_skipped")
        return claims, guardrails

    for i, c in cand:
        verdict = vmap.get(i, "")
        if verdict == "no":
            c["status"] = "unsupported"
            c.pop("unverified_numbers", None)  # 已判无支撑，数值黄标被覆盖
        elif verdict == "partial" and c.get("status") == "supported":
            c["semantic_status"] = "partial"   # 保留 supported，前端可提示部分支撑
    return claims, guardrails


# ---------- 总入口 ----------

def verify_claims(claims: list[dict], eid_map: dict[int, dict]) -> tuple[list[dict], list[str]]:
    """L2 → L3 → L4 流水；返回 (claims, guardrails)。claims 三态见模块 docstring。"""
    if not claims:
        return claims, []
    guards: list[str] = []
    claims, g2 = check_quotes(claims, eid_map)
    guards += g2
    claims, g3 = check_numbers(claims, eid_map)
    guards += g3
    claims, g4 = judge_claims(claims, eid_map)
    guards += g4
    return claims, guards
