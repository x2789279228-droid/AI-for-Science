"""多源检索测试（模块四 P0-1，密闭）：BibTeX/RIS/纯文本解析、去重合并、batch 补全、
标识符精确解析、SSE 端点。HTTP 全 mock，不打真实 API。"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SCHOLAR_DATA_DIR",
                      str(Path(__file__).parent / f"_dsc_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_API_KEY"] = ""
os.environ["MINERU_ENABLED"] = "off"
os.environ["EMBEDING_BACKEND"] = "off"
os.environ["EMBEDDING_BACKEND"] = "off"

from app import db
from app.rag import discovery


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._p


@pytest.fixture(scope="module", autouse=True)
def _setup():
    db.init_db()
    yield


# ---------- 参考文献列表解析 ----------

def test_parse_bibtex():
    bib = ("@article{bert18, title={BERT: Pre-training of Deep Bidirectional Transformers},"
           " author={Devlin, Jacob}, year={2019}, doi={10.18653/v1/N19-1423}}\n"
           "@inproceedings{vaswani17, title={Attention Is All You Need}, year={2017}}")
    out = discovery.parse_reference_list(bib)
    assert out["kind"] == "bibtex" and len(out["items"]) == 2
    assert out["items"][0]["doi"] == "10.18653/v1/N19-1423"
    assert out["items"][0]["year"] == 2019
    assert "Attention" in out["items"][1]["title"]


def test_parse_ris_and_plain():
    ris = ("TY  - JOUR\nTI  - Deep Residual Learning\nDO  - 10.1109/CVPR.2016.90\nPY  - 2016\nER  -\n")
    out = discovery.parse_reference_list(ris)
    assert out["kind"] == "ris" and out["items"][0]["doi"] == "10.1109/CVPR.2016.90"
    plain = "10.1109/CVPR.2016.90\narXiv:1810.04805\nSome Paper Title About Transformers\n"
    out2 = discovery.parse_reference_list(plain)
    assert out2["kind"] == "plain" and len(out2["items"]) == 3
    assert out2["items"][0]["doi"] and out2["items"][1]["arxiv_id"] == "1810.04805"
    assert out2["items"][2]["title"].startswith("Some Paper")


# ---------- 去重合并 ----------

def test_dedup_merges_sources_and_keeps_max_citations():
    entries = [
        {"title": "Attention Is All You Need", "authors": [], "year": 2017, "venue": "NeurIPS",
         "doi": "", "arxiv_id": "1706.03762", "citation_count": 5000, "abstract": "a",
         "sources": ["arxiv(live)"], "pdf_url": "u1"},
        {"title": "Attention is all you need", "authors": ["A Vaswani"], "year": 2017,
         "venue": "", "doi": "10.5555/3295222", "arxiv_id": "", "citation_count": 9000,
         "abstract": "", "sources": ["s2"], "pdf_url": ""},
        {"title": "An Unrelated Paper", "authors": [], "year": 2020, "venue": "", "doi": "",
         "arxiv_id": "2001.00001", "citation_count": 0, "abstract": "", "sources": ["s2"],
         "pdf_url": ""},
    ]
    got = discovery._dedup(entries)
    assert len(got) == 2, "arXiv id 与 DOI 归一标题应合并为一条"
    m = got[0]
    assert set(m["sources"]) == {"arxiv(live)", "s2"}
    assert m["citation_count"] == 9000 and m["doi"] == "10.5555/3295222"
    assert m["arxiv_id"] == "1706.03762" and m["venue"] == "NeurIPS"


# ---------- search_multi（provider mock） ----------

def test_search_multi_with_mocked_providers(monkeypatch):
    calls = []

    def fake_arxiv(q, limit):
        calls.append(("arxiv", q))
        return {"ok": True, "source": "live", "entries": [
            {"title": "Paper A", "authors": [], "year": 2023, "venue": "", "doi": "",
             "arxiv_id": "2301.00001", "citation_count": None, "abstract": "", "sources": ["arxiv(live)"], "pdf_url": ""}]}

    def fake_s2(q, limit):
        calls.append(("s2", q))
        return {"ok": False, "source": "none", "entries": [], "error": "conn"}

    monkeypatch.setattr(discovery, "_arxiv_provider", fake_arxiv)
    monkeypatch.setattr(discovery, "_s2_provider", fake_s2)
    monkeypatch.setattr(discovery, "_openalex_provider", lambda q, l: {"ok": True, "source": "live", "entries": [
        {"title": "Paper A", "authors": [], "year": 2023, "venue": "ICLR", "doi": "10.1/x",
         "arxiv_id": "", "citation_count": 12, "abstract": "", "sources": ["openalex"], "pdf_url": ""}]})
    r = discovery.search_multi("transformer", ["arxiv", "s2", "openalex"])
    assert [c[0] for c in calls] == ["arxiv", "s2"]
    assert len(r["entries"]) == 1, "arXiv id + DOI→title_norm 合并后仅 1 条"
    assert r["entries"][0]["citation_count"] == 12 and r["per_source"]["s2"]["ok"] is False


# ---------- 标识符精确解析 + batch 补全 ----------

def test_resolve_identifier_doi(monkeypatch):
    def fake_get(url, **kw):
        assert "DOI:10.18653/v1/N19-1423" in url
        return _Resp({"title": "BERT", "year": 2019, "venue": "NAACL", "citationCount": 30000,
                      "externalIds": {"DOI": "10.18653/v1/N19-1423", "ArXiv": "1810.04805"},
                      "abstract": "abs", "openAccessPdf": {"url": "https://x/p.pdf"}, "authors": []})
    monkeypatch.setattr(discovery.httpx, "get", fake_get)
    r = discovery.resolve_identifier(doi="10.18653/v1/N19-1423")
    assert r["ok"] and r["entries"][0]["arxiv_id"] == "1810.04805"


def test_resolve_reference_items_batch_and_fallback(monkeypatch):
    items = [{"raw": "a", "title": "", "doi": "10.1/a", "arxiv_id": "", "year": 0},
             {"raw": "b", "title": "Unknown Title XYZ", "doi": "", "arxiv_id": "", "year": 0}]

    def fake_post(url, **kw):
        return _Resp([{"title": "Resolved A", "year": 2020, "venue": "", "citationCount": 5,
                       "externalIds": {"DOI": "10.1/a"}, "abstract": "", "openAccessPdf": {},
                       "authors": []}])

    def fake_resolve(*, title="", doi="", arxiv_id=""):
        if "Unknown" in title:
            return {"ok": False, "error": "not_found"}
        return {"ok": True, "entries": [{"title": title, "year": 0, "venue": "", "doi": doi,
                                         "arxiv_id": "", "citation_count": 0, "abstract": "",
                                         "sources": ["s2"], "pdf_url": "", "authors": []}]}

    monkeypatch.setattr(discovery.httpx, "post", fake_post)
    monkeypatch.setattr(discovery, "resolve_identifier", fake_resolve)
    got = discovery.resolve_reference_items(items)
    assert got[0]["resolved"] is True and got[0]["title"] == "Resolved A"
    assert got[1]["resolved"] is False and got[1]["sources"] == ["unresolved"], "未解析条目如实标注"


# ---------- SSE 端点 ----------

def test_discover_search_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(discovery, "search_multi",
                        lambda q, sources, limit: {"entries": [
                            {"title": "EP", "authors": [], "year": 2024, "venue": "", "doi": "",
                             "arxiv_id": "2401.00001", "citation_count": 1, "abstract": "",
                             "sources": ["arxiv(live)"], "pdf_url": ""}],
                            "per_source": {"arxiv": {"ok": True, "source": "live", "error": ""}}})
    client = TestClient(app)
    events = []
    with client.stream("POST", "/api/discover/search", json={"query": "graph neural"}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("event: "):
                events.append(line[7:].strip())
            elif line.startswith("data: ") and "discover_results" in events[-1]:
                assert '"title": "EP"' in line or "EP" in line
    assert "status" in events and "discover_results" in events and "final" in events

    r2 = client.post("/api/discover/search", json={})
    assert r2.status_code == 400
