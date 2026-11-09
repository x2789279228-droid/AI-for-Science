"""模块三（版式感知阅读与双向联动翻译）后端支撑件单测。

覆盖：
- GET /papers/{id}/search：FTS 命中/snippet/block_ids/400（短查询）/404（未解析）/
  空结果不 404（直接调路由函数——无鉴权层，TestClient 不增加断言面）
- pipeline._raw_order_perm：原始序置换纯函数（meta.raw_orders 的数据基础，
  perm[k]=原始序第 k 块在重排后数组中的索引，恒等不记）
- translate.load_blocks：与 docir 端点同源的 docir_path 回退（非默认路径可译）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import db, translate
from app.api_routes import routes
from app.config import DOCIR_DIR
from app.parsing.pipeline import _raw_order_perm
from app.rag import indexing

# ---------------- _raw_order_perm（E：真实重排记录） ----------------


def test_raw_order_perm_permutation_and_identity():
    a, b, c = {"t": "A"}, {"t": "B"}, {"t": "C"}
    perm = _raw_order_perm([a, b, c], [b, c, a])
    assert perm == [2, 0, 1]
    # 还原语义：按 perm 依次取重排数组 → 原始顺序
    reordered = [b, c, a]
    assert [reordered[i] for i in perm] == [a, b, c]
    assert _raw_order_perm([a, b, c], [a, b, c]) is None   # 恒等（未重排）不记
    assert _raw_order_perm([a], [a]) is None               # 单块无重排语义
    assert _raw_order_perm([a, b], [a]) is None            # 长度不一致防御


# ---------------- 搜索端点（D） ----------------


def _seed_paper(pid: str, texts: list[str], *, docir_path: Path | None = None) -> None:
    """paper 行 + DocIR JSON + chunks/FTS 行（EMBEDDING_BACKEND=off → 零外呼）。"""
    path = docir_path or (Path(DOCIR_DIR) / f"{pid}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = [{"id": f"b{i + 1}", "type": "paragraph", "text": t, "page": i,
               "bbox": [0, 0, 1, 1], "section_path": "1 Intro"}
              for i, t in enumerate(texts)]
    path.write_text(json.dumps({"meta": {"title": "T"}, "toc": [], "blocks": blocks,
                                "tables": [], "figures": [], "references": []}),
                    encoding="utf-8")
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO papers(id,title,file_path,file_hash,docir_path,"
                  "created_at) VALUES(?,?,?,?,?,?)",
                  (pid, "T", "x.pdf", f"h_{pid}", str(path), time.time()))
    chunks = [{"text": t, "page": i, "bbox": [0, 0, 1, 1], "section_path": "1 Intro",
               "block_ids": [f"b{i + 1}"], "kind": "detail"} for i, t in enumerate(texts)]
    indexing.index_chunks(pid, chunks)


def test_search_endpoint_hit_snippet_and_block_ids():
    pid = "p_srch_hit"
    _seed_paper(pid, [
        "The transformer attention mechanism is effective for machine translation.",
        "We evaluate on WMT14 English-German with BLEU 28.4.",
    ])
    out = routes.search_paper(pid, "attention mechanism")
    assert out["total"] >= 1
    r = out["results"][0]
    assert "attention" in r["snippet"].lower()
    assert r["page"] == 0 and r["section_path"] == "1 Intro"
    assert isinstance(r["block_ids"], list) and r["block_ids"]


def test_search_endpoint_chinese_bigram():
    pid = "p_srch_cjk"
    _seed_paper(pid, ["注意力机制是Transformer的核心组件。", "模型在翻译任务上表现良好。"])
    out = routes.search_paper(pid, "注意力")
    assert out["total"] >= 1
    assert "注意" in out["results"][0]["snippet"]


def test_search_endpoint_snippet_window_has_ellipsis():
    pid = "p_srch_win"
    long_text = ("word " * 200) + "needle" + (" word" * 200)
    _seed_paper(pid, [long_text])
    r = routes.search_paper(pid, "needle")["results"][0]
    assert "needle" in r["snippet"] and "…" in r["snippet"]


def test_search_endpoint_guards():
    # 未解析（无 DocIR）→ 404（与 docir 端点同语义：存在性判在 q 校验之前）
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO papers(id,title,file_path,file_hash,docir_path,"
                  "created_at) VALUES(?,?,?,?,?,?)",
                  ("p_srch_bare", "T", "x.pdf", "h1", "", time.time()))
    with pytest.raises(HTTPException) as e2:
        routes.search_paper("p_srch_bare", "hello")
    assert e2.value.status_code == 404
    # 已解析论文 q 过短 → 400
    pid = "p_srch_short"
    _seed_paper(pid, ["alpha beta gamma"])
    with pytest.raises(HTTPException) as e1:
        routes.search_paper(pid, " a ")
    assert e1.value.status_code == 400
    # 无命中 → 空列表（不 404）
    out = routes.search_paper(pid, "zzzqqqxxx")
    assert out == {"results": [], "total": 0}


def test_search_endpoint_top_k_clamp():
    pid = "p_srch_k"
    _seed_paper(pid, [f"clip item number {i}" for i in range(5)])
    out = routes.search_paper(pid, "clip", top_k=999)   # 上限 50 防御（5 条全命中）
    assert out["total"] == 5


# ---------------- translate.load_blocks 回退（G 顺手小修） ----------------


def test_load_blocks_custom_docir_path(tmp_path: Path):
    custom = tmp_path / "elsewhere" / "p_tr1.json"
    _seed_paper("p_tr1", ["hello world"], docir_path=custom)
    assert not (Path(DOCIR_DIR) / "p_tr1.json").exists()   # 确认走的是非默认路径
    data = translate.load_blocks("p_tr1")
    assert data["blocks"][0]["text"] == "hello world"


def test_load_blocks_missing_raises():
    with pytest.raises(FileNotFoundError):
        translate.load_blocks("p_tr_nonexistent")
