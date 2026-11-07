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
    for sp in order:
        sec_blocks = sections[sp]
        buf: list[dict] = []
        buf_len = 0

        def flush() -> None:
            nonlocal buf, buf_len
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
                    "section_path": sp,
                    "block_ids": [b["id"] for b in buf],
                    "kind": "detail",
                })
            buf, buf_len = [], 0

        for blk in sec_blocks:
            if blk["type"] == "heading":
                continue
            text = blk["text"]
            # 超长段内切（句子边界）
            for piece in _split_long(text):
                if buf_len + len(piece) > TARGET and buf_len > MIN_LEN:
                    flush()
                    # 重叠：把上一块尾部 100 字符带入（近似：重复利用最后一块文本）
                    if chunks:
                        tail = chunks[-1]["text"][-OVERLAP:]
                        buf.append({"id": "ovl", "text": tail, "page": chunks[-1]["page"],
                                    "bbox": chunks[-1]["bbox"]})
                        buf_len = len(tail)
                buf.append(blk if piece is text else {**blk, "text": piece})
                buf_len += len(piece)
        flush()

    # 章节摘要块：每个 level-1 章节生成一条概览（页范围）
    for t in docir.get("toc", []):
        if t.get("level") != 1:
            continue
        sec_chunks = [c for c in chunks if c["section_path"] == t["title"]]
        if sec_chunks:
            chunks.append({"text": f"[章节概览] {t['title']}（pp.{sec_chunks[0]['page']}-{sec_chunks[-1]['page']}）："
                                   + sec_chunks[0]["text"][:300],
                           "page": t["page"], "pages": [t["page"]],
                           "bbox": sec_chunks[0]["bbox"], "section_path": t["title"],
                           "block_ids": [], "kind": "summary"})
    return chunks


_SENT = re.compile(r"(?<=[.!?。！？；;])\s+")


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
