"""结构化输出 Pydantic schema（4.5）：LLM JSON 出口全部经 model_validate 校验。

宽松入参原则：evidence_ids 兼容 1 / 1.0 / "1" / "E1"（对齐 rag.answering._parse_eid 的
实测教训——LLM 偶发把编号输出成字符串，校验器负责兜住而非拒收）。
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


def _coerce_eid(x: object) -> int | None:
    """1 / 1.0 / "1" / "E1" → int；其他 → None（静默丢弃）。"""
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, float) and x.is_integer():
        return int(x)
    if isinstance(x, str):
        m = re.search(r"\d+", x)
        if m:
            return int(m.group())
    return None


class Claim(BaseModel):
    text: str = Field(min_length=1)
    evidence_ids: list[int] = Field(default_factory=list)
    quote: str = ""

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _coerce_ids(cls, v: object) -> list[int]:
        if not isinstance(v, list):
            return []
        out, seen = [], set()
        for item in v:
            n = _coerce_eid(item)
            if n is not None and n not in seen:
                seen.add(n)
                out.append(n)
        return out


class ClaimList(BaseModel):
    claims: list[Claim] = Field(default_factory=list)


class QuoteMap(BaseModel):
    quotes: dict[str, str] = Field(default_factory=dict)


class Verdict(BaseModel):
    id: int
    verdict: str  # yes | partial | no


class VerdictList(BaseModel):
    verdicts: list[Verdict] = Field(default_factory=list)


# ---- 4.6 图谱洞察 ----

class CitationClass(BaseModel):
    idx: int
    cls: str = "perfunctory"   # substantive | background | perfunctory


class CitationClassList(BaseModel):
    classes: list[CitationClass] = Field(default_factory=list)


class FutureWork(BaseModel):
    idea_text: str = Field(min_length=1)


class FutureWorkList(BaseModel):
    ideas: list[FutureWork] = Field(default_factory=list)


class IdeaVerdict(BaseModel):
    id: int
    verdict: str = "open"      # solved | partial | open
    reason: str = ""


class IdeaVerdictList(BaseModel):
    verdicts: list[IdeaVerdict] = Field(default_factory=list)
