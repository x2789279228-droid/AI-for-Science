"""索引层：SQLite FTS5（Sparse/BM25，必开）+ Dense（向量在索引时计算并落库）。

Dense 后端三选一（EMBEDDING_BACKEND）：
- api   ：OpenAI 兼容 /embeddings（SiliconFlow BGE-M3 / 方舟 doubao-embedding-vision）
- local ：fastembed 本地 BGE-M3（零 API 依赖）
- off   ：纯 BM25
无向量落库时该 paper 的 Dense 通道自动关闭，检索管线不变（RRF 单路=退化为排序）。
"""
from __future__ import annotations

import json
import struct
import time
from typing import Any

import httpx
import numpy as np

from .. import db
from ..config import settings

_MODEL_CACHE: dict[str, Any] = {}
_EMBED_LAST_ERROR: str = ""  # 最近一次向量化失败原因（诊断用，任务消息会带出）


# ---------- 本地 fastembed ----------

def _local_model():
    key = f"local:{settings.embedding_model}"
    if key not in _MODEL_CACHE:
        from fastembed import TextEmbedding
        _MODEL_CACHE[key] = TextEmbedding(model_name=settings.embedding_model)
    return _MODEL_CACHE[key]


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype="<f4")


# ---------- 向量化（索引时） ----------

def _backend_mode() -> str:
    backend = settings.embedding_backend
    if backend in ("api", "local", "off"):
        return backend
    return "api" if settings.embedding_api_key else ("local" if settings.dense_enabled else "off")


def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """按后端编码文本；失败返回 None（该批无向量，Dense 自动降级）。

    API 后端：批 8（方舟 Embeddings 单批上限 10）+ 指数退避重试
    （订阅套餐按请求计积分，连发会触发限流，必须留间隔）。
    """
    global _EMBED_LAST_ERROR
    mode = _backend_mode()
    print(f"[embed] mode={mode} n={len(texts)} model={settings.embedding_model}", flush=True)
    if mode == "local":
        try:
            model = _local_model()
            return [list(v) for v in model.embed(texts)]
        except Exception as e:
            _EMBED_LAST_ERROR = f"local:{type(e).__name__}: {e}"
            return None
    if mode == "api" and settings.embedding_api_key:
        out: list[list[float]] = []
        for i in range(0, len(texts), 8):
            batch = texts[i:i + 8]
            ok = False
            for attempt in range(3):
                try:
                    resp = httpx.post(
                        f"{settings.embedding_base_url.rstrip('/')}/embeddings",
                        headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
                        json={"model": settings.embedding_model, "input": batch},
                        timeout=120,
                    )
                    if resp.status_code == 429:  # 限流：退避后重试
                        _EMBED_LAST_ERROR = f"api:429 rate_limited (batch {i // 8})"
                        print(f"[embed] batch {i // 8} attempt {attempt}: {_EMBED_LAST_ERROR}", flush=True)
                        time.sleep(2.0 * (attempt + 1))
                        continue
                    resp.raise_for_status()
                    data = sorted(resp.json()["data"], key=lambda d: d["index"])
                    out.extend(d["embedding"] for d in data)
                    ok = True
                    break
                except Exception as e:
                    _EMBED_LAST_ERROR = f"api:{type(e).__name__}: {e}"[:200]
                    print(f"[embed] batch {i // 8} attempt {attempt} EXC: {_EMBED_LAST_ERROR}", flush=True)
                    time.sleep(1.0 * (attempt + 1))
            if not ok:
                return None  # 任一批重试仍败 → 整批降级，由调用方如实报告
            time.sleep(0.3)  # 批间隔，避开连发限流
        return out
    return None


def index_chunks(paper_id: str, chunks: list[dict]) -> int:
    texts = [ch["text"] for ch in chunks]
    vecs = _embed_texts(texts) if chunks else None
    with db.tx() as c:
        c.execute("DELETE FROM chunks WHERE paper_id=?", (paper_id,))
        c.execute("DELETE FROM chunks_fts WHERE paper_id=?", (paper_id,))
        for seq, ch in enumerate(chunks):
            cid = db.new_id("c_")
            vec_blob = _pack(vecs[seq]) if vecs else None
            c.execute(
                "INSERT INTO chunks(id,paper_id,seq,text,page,bbox_json,section_path,"
                "block_ids,kind,vec) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cid, paper_id, seq, ch["text"], ch["page"], json.dumps(ch["bbox"]),
                 ch["section_path"], json.dumps(ch["block_ids"]), ch["kind"], vec_blob),
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


def dense_search(paper_ids: list[str], q: str, top_k: int = 20) -> list[dict]:
    """Dense 检索：query 向量 × 库内落库向量（索引时已算好，查询零重算）。"""
    if not paper_ids:
        return []
    qvecs = _embed_texts([q])
    if not qvecs:
        return []
    qv = np.asarray(qvecs[0], dtype="float32")
    qv /= (np.linalg.norm(qv) + 1e-9)

    ph = ",".join("?" * len(paper_ids))
    rows = db.query(f"SELECT id AS chunk_id,paper_id,text,page,bbox_json,section_path,block_ids,kind,vec"
                    f" FROM chunks WHERE paper_id IN ({ph}) AND vec IS NOT NULL", tuple(paper_ids))
    if not rows:
        return []
    mat = np.stack([_unpack(r["vec"]) for r in rows]).astype("float32")
    if mat.shape[1] != len(qv):  # 换过 embedding 模型：维度不匹配 → 关 dense
        return []
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    sims = mat @ qv
    order = np.argsort(-sims)[:top_k]
    return [_row_to_hit(rows[i], score=float(sims[i])) for i in order]


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
