"""Section-aware 切块（方案 §4.2）：按标题层级切，~900 字符，重叠 100，
每块携带溯源锚点 (paper_id, section_path, page, bbox)。"""
from __future__ import annotations

import re
from typing import Any

TARGET = 900
OVERLAP = 100
MIN_LEN = 40


def chunk_docir(docir: dict[str, Any]) -> list[dict]:
    """DocIR blocks → chunks。摘要级（level-1 标题下的首段）标记 kind=summary 供章节概览。"""
    blocks = docir.get("blocks", [])
    if not blocks:
        return []
    # 按 section 分组，保持阅读顺序
    sections: dict[str, list[dict]] = {}
    order: list[str] = []
    for blk in blocks:
        sp = blk.get("section_path") or "_front"
        if sp not in sections:
            sections[sp] = []
            order.append(sp)
        sections[sp].append(blk)

    chunks: list[dict] = []
    last_real_id = ""   # Round6：上一 flush 的最后真实块 id（重叠尾巴的锚）
    for sp in order:
        sec_blocks = sections[sp]
        buf: list[dict] = []
        buf_len = 0

        def flush(section: str = sp) -> None:
            nonlocal buf, buf_len, last_real_id
            if not buf:
                return
            text = "\n".join(b["text"] for b in buf).strip()
            if len(text) >= MIN_LEN:
                pages = sorted({b["page"] for b in buf})
                chunks.append({
                    "text": text,
                    "page": pages[0],
                    "pages": pages,
                    "bbox": buf[0]["bbox"],
                    "section_path": section,
                    "block_ids": list(dict.fromkeys(b["id"] for b in buf if b.get("id"))),
                    "kind": "detail",
                })
                # Round6：记最后真实块 id——下一块的重叠尾巴文本出自它。取上一
                # chunk 的 block_ids[-1] 会漏锚（其 ids 可能为空，如标题后首块）
                last_real_id = next((b["id"] for b in reversed(buf) if b.get("id")),
                                    last_real_id)
            buf, buf_len = [], 0

        for blk in sec_blocks:
            if blk["type"] == "heading":
                continue
            text = blk["text"]
            # 超长段内切（句子边界）
            for piece in _split_long(text):
                if buf_len + len(piece) > TARGET and buf_len > MIN_LEN:
                    n_before = len(chunks)
                    flush()
                    # 重叠：把上一块尾部 100 字符带入（挂 last_real_id——重叠文本
                    # 出自该块）。Round6 守卫：仅当本次 flush **真正产出**才带重叠
                    # （短文本被丢弃时 chunks[-1] 指向旧块，尾巴来源错位使该 chunk
                    # 的块锚覆盖系统性 <0.85——Attention 5/73 实证）
                    if len(chunks) > n_before:
                        tail = chunks[-1]["text"][-OVERLAP:]
                        buf.append({"id": last_real_id,
                                    "text": tail, "page": chunks[-1]["page"],
                                    "bbox": chunks[-1]["bbox"]})
                        buf_len = len(tail)
                buf.append(blk if piece is text else {**blk, "text": piece})
                buf_len += len(piece)
        flush()

    # 表格块：每表一个 chunk（markdown 原文），数值/超参类问题靠它被 BM25 精确命中
    for t in docir.get("tables", []):
        md = (t.get("markdown") or "").strip()
        if len(md) < MIN_LEN:
            continue
        chunks.append({
            "text": f"[表格 {t.get('id', '')}]\n{md[:4000]}",
            "page": t.get("page", 0),
            "pages": [t.get("page", 0)],
            "bbox": t.get("bbox") or [],
            "section_path": _table_section(docir, t),
            "block_ids": [t.get("id", "")] if t.get("id") else [],
            "kind": "table",
        })

    # 章节摘要块：每个 level-1 章节生成一条概览（页范围）
    for t in docir.get("toc", []):
        if t.get("level") != 1:
            continue
        sec_chunks = [c for c in chunks if c["section_path"] == t["title"]]
        if sec_chunks:
            # Round6：概览块回填该章块 id 并集（cap 8）——概览 text 取自首块正文，
            # 空 block_ids 使其命中时溯源无锚（round5 cell_quote 残余之一）
            _ids: list[str] = []
            for c0 in sec_chunks:
                for b0 in c0["block_ids"]:
                    if b0 not in _ids:
                        _ids.append(b0)
                    if len(_ids) >= 8:
                        break
                if len(_ids) >= 8:
                    break
            chunks.append({"text": f"[章节概览] {t['title']}（pp.{sec_chunks[0]['page']}-{sec_chunks[-1]['page']}）："
                                   + sec_chunks[0]["text"][:300],
                           "page": t["page"], "pages": [t["page"]],
                           "bbox": sec_chunks[0]["bbox"], "section_path": t["title"],
                           "block_ids": _ids, "kind": "summary"})
    return chunks


_SENT = re.compile(r"(?<=[.!?。！？；;])\s+")


def _table_section(docir: dict[str, Any], table: dict[str, Any]) -> str:
    """表格归属章节：取阅读顺序中最后一个页码不晚于表格所在页的 block 的 section_path。"""
    page = table.get("page", 0)
    sec = ""
    for blk in docir.get("blocks", []):
        if blk.get("page", 0) > page:
            break
        if blk.get("section_path"):
            sec = blk["section_path"]
    return sec or "前言"


def _split_long(text: str, max_len: int = TARGET) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts, cur = [], ""
    for sent in _SENT.split(text):
        while len(sent) > max_len:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(sent[:max_len])
            sent = sent[max_len:]
        if len(cur) + len(sent) > max_len and cur:
            parts.append(cur)
            cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if cur:
        parts.append(cur)
    return parts
