"""索引层：SQLite FTS5（Sparse/BM25，必开）+ Dense（API embedding 可选）。

Dense 通过 OpenAI 兼容 /embeddings 接口（SiliconFlow BGE-M3 等）；
无 Key 时自动降级为纯 BM25，检索管线不变（RRF 单路=退化为排序）。
"""
from __future__ import annotations

import json
import math
from typing import Any

import httpx

from .. import db
from ..config import settings


# ---------- FTS5 ----------

def index_chunks(paper_id: str, chunks: list[dict]) -> int:
    with db.tx() as c:
        c.execute("DELETE FROM chunks WHERE paper_id=?", (paper_id,))
        c.execute("DELETE FROM chunks_fts WHERE paper_id=?", (paper_id,))
        for seq, ch in enumerate(chunks):
            cid = db.new_id("c_")
            c.execute(
                "INSERT INTO chunks(id,paper_id,seq,text,page,bbox_json,section_path,block_ids,kind)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (cid, paper_id, seq, ch["text"], ch["page"], json.dumps(ch["bbox"]),
                 ch["section_path"], json.dumps(ch["block_ids"]), ch["kind"]),
            )
            c.execute("INSERT INTO chunks_fts(chunk_id,paper_id,text) VALUES(?,?,?)",
                      (cid, paper_id, ch["text"]))
    return len(chunks)


def fts_search(paper_ids: list[str], q: str, top_k: int = 20) -> list[dict]:
    """BM25 检索。FTS5 查询词做安全转义；多词 OR 提高召回。"""
    if not paper_ids:
        return []
    terms = [t for t in "".join(ch if ch.isalnum() else " " for ch in q).split() if t][:8]
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    ph = ",".join("?" * len(paper_ids))
    try:
        rows = db.query(
            f"SELECT f.chunk_id, f.paper_id, bm25(chunks_fts) AS score, c.text, c.page,"
            f" c.bbox_json, c.section_path, c.block_ids, c.kind"
            f" FROM chunks_fts f JOIN chunks c ON c.id=f.chunk_id"
            f" WHERE chunks_fts MATCH ? AND f.paper_id IN ({ph})"
            f" ORDER BY score LIMIT ?",
            (match, *paper_ids, top_k),
        )
    except Exception:
        return []
    return [_row_to_hit(r, score=-r["score"]) for r in rows]


def _row_to_hit(r: Any, score: float) -> dict:
    return {"chunk_id": r["chunk_id"], "paper_id": r["paper_id"], "text": r["text"],
            "page": r["page"], "bbox": json.loads(r["bbox_json"] or "[]"),
            "section_path": r["section_path"], "block_ids": json.loads(r["block_ids"] or "[]"),
            "kind": r["kind"], "score": round(float(score), 4)}


# ---------- Dense（OpenAI 兼容 embeddings） ----------

def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """调用 /embeddings；失败返回 None（自动降级）。"""
    if not settings.embedding_api_key:
        return None
    try:
        resp = httpx.post(
            f"{settings.embedding_base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
            json={"model": settings.embedding_model, "input": texts[:32]},
            timeout=30,
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]
    except Exception:
        return None


def dense_search(paper_ids: list[str], q: str, top_k: int = 20) -> list[dict]:
    """Dense 检索：query 向量 × 库内 chunk 向量（向量即时编码落库内存缓存）。"""
    if not paper_ids:
        return []
    qvecs = _embed_texts([q])
    if not qvecs:
        return []
    import numpy as np
    qv = np.asarray(qvecs[0], dtype="float32")
    qv /= (np.linalg.norm(qv) + 1e-9)

    ph = ",".join("?" * len(paper_ids))
    rows = db.query(f"SELECT id,paper_id,text,page,bbox_json,section_path,block_ids,kind"
                    f" FROM chunks WHERE paper_id IN ({ph})", tuple(paper_ids))
    if not rows:
        return []
    chunk_texts = [r["text"] for r in rows]
    vecs = _embed_texts(chunk_texts)
    if not vecs:
        return []
    mat = np.asarray(vecs, dtype="float32")
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    sims = mat @ qv
    order = np.argsort(-sims)[:top_k]
    hits = []
    for i in order:
        hits.append(_row_to_hit(rows[i], score=float(sims[i])))
    return hits


# ---------- RRF 融合 ----------

def rrf_fuse(*channel_lists: tuple[str, list[dict]], k: int = 60, top_k: int = 20) -> list[dict]:
    """Reciprocal Rank Fusion：多路检索结果按 rank 融合，保留元数据与来源通道。"""
    scores: dict[str, float] = {}
    meta: dict[str, dict] = {}
    for channel, lst in channel_lists:
        for rank, hit in enumerate(lst):
            cid = hit["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            if cid not in meta:
                meta[cid] = {**hit, "sources": []}
            meta[cid]["sources"].append({"channel": channel, "rank": rank, "score": hit["score"]})
    fused = sorted(meta.values(), key=lambda h: -scores[h["chunk_id"]])[:top_k]
    for h in fused:
        h["rrf"] = round(scores[h["chunk_id"]], 6)
    return fused


def hybrid_search(paper_ids: list[str], q: str, top_k: int = 20) -> dict:
    """混合检索入口：BM25 +（可选）Dense → RRF。返回 hits + 诊断信息。"""
    bm25 = fts_search(paper_ids, q, top_k)
    dense = dense_search(paper_ids, q, top_k) if settings.dense_enabled else []
    channels = ["bm25"] + (["dense"] if dense else [])
    hits = rrf_fuse(("bm25", bm25), ("dense", dense), top_k=top_k) if dense else bm25
    return {"hits": hits, "channels": channels, "bm25_count": len(bm25), "dense_count": len(dense)}
