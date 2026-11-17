"""一次性脚本（v2.3.0 重建收尾）：限流友好的慢速重嵌入。

方舟 plan 端点对密集连发限流（0.3s 批间隔下 429 连撞）；本脚本批间 1.2s、
失败批不重试整篇（该批 vec 置 NULL，之后可用 backfill_embeddings.py 补）。
两步：
  1) 仍为旧分块的论文：重跑 chunk_docir + 分批嵌入 + 落库（镜像 index_chunks）
  2) 已重分块但 vec 全空的论文：逐批补嵌入 UPDATE
用法：cd api && uv run python scripts/_reindex_slow.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db
from app.config import DOCIR_DIR, settings
from app.jobs import _index_view
from app.rag.chunking import chunk_docir
from app.rag.cjk import cjk_bigram
from app.rag.indexing import _embed_texts, _pack

BATCH = 8
PAUSE = 1.2

# 仍为旧分块的论文（reindex_chunks.py --dry 对照得出）
STALE = ["p_62cbcc27247e", "p_cc0c091c450b", "p_1027fe244980", "p_07f89969ed4f",
         "p_00771102f400", "p_5b5eeecf4c8a", "p_f33df402c407", "p_e8177ae1ece9",
         "p_94db928f9476", "p_fc5094c7c36b", "p_726220983df7"]
# 已重分块但 vec 空（上一轮限流损伤）
BACKFILL = ["p_1264458b503d", "p_9387136178c3", "p_125e75386f44"]


def embed_slow(texts: list[str]) -> list[list[float] | None]:
    out: list[list[float] | None] = []
    fails = 0
    for i in range(0, len(texts), BATCH):
        batch = texts[i:i + BATCH]
        v = _embed_texts(batch)
        if v:
            out.extend(v)
        else:
            out.extend([None] * len(batch))
            fails += 1
        time.sleep(PAUSE)
    if fails:
        print(f"    warn: {fails} 批嵌入失败（vec 置 NULL，可 backfill 补）")
    return out


def reindex(pid: str) -> None:
    path = DOCIR_DIR / f"{pid}.json"
    docir = json.loads(path.read_text(encoding="utf-8"))
    if (docir.get("meta") or {}).get("long_doc"):
        docir = _index_view(docir, settings.long_doc_index_pages)
    chunks = chunk_docir(docir)
    print(f"{pid}: chunk {len(chunks)}")
    vecs = embed_slow([c["text"] for c in chunks])
    with db.tx() as c:
        c.execute("DELETE FROM chunks WHERE paper_id=?", (pid,))
        c.execute("DELETE FROM chunks_fts WHERE paper_id=?", (pid,))
        for seq, ch in enumerate(chunks):
            cid = db.new_id("c_")
            blob = _pack(vecs[seq]) if vecs[seq] else None
            c.execute("INSERT INTO chunks(id,paper_id,seq,text,text_cjk,page,bbox_json,"
                      "section_path,block_ids,kind,vec) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (cid, pid, seq, ch["text"], cjk_bigram(ch["text"]), ch["page"],
                       json.dumps(ch["bbox"]), ch["section_path"], json.dumps(ch["block_ids"]),
                       ch["kind"], blob))
            c.execute("INSERT INTO chunks_fts(chunk_id,paper_id,text_cjk) VALUES(?,?,?)",
                      (cid, pid, cjk_bigram(ch["text"])))
    n = db.query("SELECT COUNT(*) n, SUM(vec IS NOT NULL) v FROM chunks WHERE paper_id=?",
                 (pid,))[0]
    print(f"    done: {n['n']} chunks, vec {n['v']}")


def backfill(pid: str) -> None:
    rows = db.query("SELECT id, text FROM chunks WHERE paper_id=? AND vec IS NULL "
                    "ORDER BY seq", (pid,))
    if not rows:
        print(f"{pid}: 无缺向量")
        return
    print(f"{pid}: backfill {len(rows)} vec")
    vecs = embed_slow([r["text"] for r in rows])
    with db.tx() as c:
        for r, v in zip(rows, vecs):
            if v:
                c.execute("UPDATE chunks SET vec=? WHERE id=?", (_pack(v), r["id"]))
    n = db.query("SELECT COUNT(*) n, SUM(vec IS NOT NULL) v FROM chunks WHERE paper_id=?",
                 (pid,))[0]
    print(f"    done: vec {n['v']}/{n['n']}")


def main() -> int:
    db.init_db()
    for pid in STALE:
        reindex(pid)
    for pid in BACKFILL:
        backfill(pid)
    total = db.query("SELECT COUNT(*) n, SUM(vec IS NOT NULL) v FROM chunks")[0]
    print(f"\n== 总计 chunks {total['n']} / vec {total['v']} ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
