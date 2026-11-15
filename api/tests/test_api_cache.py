"""性能修复验证：热点 GET 服务端缓存 + ETag/304 + revision 失效 + DocIR 文件缓存不变异。

对应优化（网页卡顿修复）：
- /papers /families /graph 二次请求命中进程内 TTL 缓存（响应带 cached=true）
- ETag + If-None-Match → 304（命中元信息不进 ETag，避免抖动）
- 上传（bump revision）→ 列表缓存立即失效（cached=false）
- /docir 文件缓存：翻页不再全量读盘；页过滤不得污染缓存 dict（outline 不变量）
"""
from __future__ import annotations

import hashlib
import io
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app import cache

_DATA = Path(__import__("os").environ["SCHOLAR_DATA_DIR"])


def _client() -> TestClient:
    from app.main import app

    return TestClient(app)


def _make_pdf(path: Path, title: str) -> Path:
    from tests.fixtures_pdf import make_paper_pdf

    tag = hashlib.md5(title.encode("utf-8")).hexdigest()[:10]
    make_paper_pdf(path, title,
                   body_facts=[f"fact-{tag}: recall 88.0 percent."],
                   refs=[f"Marker-{tag} et al. 2024. arXiv:1706.03762"],
                   arxiv_line=f"arXiv:2401.{int(tag[:6], 16) % 100000:05d}v1")
    return path


def _upload(client: TestClient, path: Path) -> dict:
    r = client.post("/api/papers",
                    files={"file": (path.name, io.BytesIO(path.read_bytes()), "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()


def _wait(client: TestClient, job_id: str, timeout_s: float = 120) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            return j
        time.sleep(0.2)
    return {"status": "timeout"}


def test_etag_304_roundtrip():
    with _client() as c:
        r1 = c.get("/api/papers")
        assert r1.status_code == 200
        etag = r1.headers.get("etag")
        assert etag, "热点 GET 应返回 ETag"
        assert r1.headers.get("cache-control") == "no-cache"
        r2 = c.get("/api/papers", headers={"If-None-Match": etag})
        assert r2.status_code == 304, "内容未变时应 304"
        assert not (r2.content or b"").strip(), "304 不应携带响应体"
        # 命中元信息不参与 ETag：cached 值变化不改变 ETag（重验证稳定命中）
        r3 = c.get("/api/papers")
        assert r3.status_code == 200
        assert r3.headers.get("etag") == etag
        r4 = c.get("/api/papers", headers={"If-None-Match": etag})
        assert r4.status_code == 304


def test_papers_cache_hit_and_revision_invalidation():
    p = _make_pdf(_DATA / "cache_inv.pdf", "缓存失效验证论文")
    with _client() as c:
        r1 = c.get("/api/papers", params={"doc_type": "paper"})
        assert r1.json()["cached"] is False          # 首次构建
        r2 = c.get("/api/papers", params={"doc_type": "paper"})
        assert r2.json()["cached"] is True           # TTL 内命中
        assert r2.json()["cache_age"] >= 0

        up = _upload(c, p)                           # 写路径 bump revision
        r3 = c.get("/api/papers", params={"doc_type": "paper"})
        assert r3.json()["cached"] is False          # revision 变化 → 重建
        assert up["paper_id"] in {x["id"] for x in r3.json()["papers"]}


def test_families_cache_hit():
    with _client() as c:
        r1 = c.get("/api/families")
        assert r1.status_code == 200
        r2 = c.get("/api/families")
        assert r2.json()["cached"] is True


def test_graph_cache_hit_and_payload_shape():
    p = _make_pdf(_DATA / "cache_graph.pdf", "图谱缓存验证论文")
    with _client() as c:
        up = _upload(c, p)
        assert _wait(c, up["job_id"])["status"] == "done"
        r1 = c.get("/api/graph")
        assert r1.status_code == 200
        g1 = r1.json()
        assert "cached" in g1 and g1["cached"] is False
        assert g1["insights"]["cornerstone"] is not None   # 原载荷字段保留
        r2 = c.get("/api/graph")
        assert r2.json()["cached"] is True                 # 300s TTL 内命中（不再重算/不再触发 LLM 分类）


def test_docir_file_cache_and_no_mutation():
    """DocIR 文件缓存：翻页后 outline 的 block_count 不变（页过滤不得污染缓存）。"""
    p = _make_pdf(_DATA / "cache_docir.pdf", "DocIR缓存验证手册")
    with _client() as c:
        up = _upload(c, p)
        assert _wait(c, up["job_id"])["status"] == "done"
        pid = up["paper_id"]
        o1 = c.get(f"/api/papers/{pid}/docir", params={"outline": "1"}).json()
        assert o1["block_count"] > 0
        pg = c.get(f"/api/papers/{pid}/docir", params={"page": 0}).json()
        assert all(b["page"] == 0 for b in pg["blocks"])   # 页过滤生效
        o2 = c.get(f"/api/papers/{pid}/docir", params={"outline": "1"}).json()
        assert o2["block_count"] == o1["block_count"], "页过滤不得污染缓存 dict"
        # 文件缓存命中：直接打服务函数，第二次应命中同一对象（mtime+size 键）
        from app.api_routes.routes import _resolve_paper_data
        d1 = _resolve_paper_data(pid)
        d2 = _resolve_paper_data(pid)
        assert d1 is d2, "mtime 未变时应命中同一缓存对象"


def test_revision_bump_on_delete():
    p = _make_pdf(_DATA / "cache_del.pdf", "删除失效验证论文")
    with _client() as c:
        up = _upload(c, p)
        pid = up["paper_id"]
        c.get("/api/papers", params={"doc_type": "paper"})   # 预热缓存
        r = c.get("/api/papers", params={"doc_type": "paper"})
        assert r.json()["cached"] is True
        before = cache.get_revision()
        d = c.delete(f"/api/papers/{pid}")
        assert d.status_code == 200
        assert cache.get_revision() > before
        r2 = c.get("/api/papers", params={"doc_type": "paper"})
        assert r2.json()["cached"] is False
        assert pid not in {x["id"] for x in r2.json()["papers"]}
