"""模块一 P0 分层元数据抽取：启发式摘要 / LLM 层（mock）/ arXiv API 层（mock+缓存+降级）。"""
from __future__ import annotations

import json

from app import db
from app.rag import metadata

# ---------- 第 3 层：启发式摘要 ----------

def _docir_with_abstract() -> dict:
    return {"meta": {"arxiv_id": ""}, "blocks": [
        {"type": "heading", "text": "Attention Is All You Need", "page": 0},
        {"type": "paragraph", "text": "Anna Author, Bob Chen, Carol Li", "page": 0},
        {"type": "heading", "text": "Abstract", "page": 0},
        {"type": "paragraph",
         "text": "We propose the Transformer, the first sequence model based solely on attention.",
         "page": 0},
        {"type": "paragraph",
         "text": "It achieves state-of-the-art results on machine translation in 26.1 BLEU.",
         "page": 0},
        {"type": "heading", "text": "1 Introduction", "page": 0},
        {"type": "paragraph", "text": "Recurrent networks dominated sequence modeling.", "page": 0},
    ]}


def test_heuristic_abstract_stops_at_introduction():
    docir = _docir_with_abstract()
    abstract = metadata.heuristic_abstract(docir)
    assert "Transformer" in abstract and "26.1 BLEU" in abstract
    assert "Recurrent" not in abstract, "Introduction 起必须截断"


def test_first_page_text_bounded():
    docir = _docir_with_abstract()
    docir["blocks"].append({"type": "paragraph", "text": "x" * 5000, "page": 5})
    text = metadata.first_page_text(docir)
    assert len(text) <= 3000
    assert "x" * 100 not in text, "第 6 页内容不进首页文本"


# ---------- 第 2 层：LLM（mock） ----------

def test_llm_extract_parses_and_filters(monkeypatch):
    def fake_chat_json_validated(messages, model_cls, **kw):
        return {"authors": [{"name": " Anna Author ", "affiliation": "Google Brain"},
                            {"name": "", "affiliation": ""}],
                "abstract": "LLM abstract.", "venue": "NeurIPS", "year": 2017, "doi": ""}

    monkeypatch.setattr(metadata.client, "chat_json_validated", fake_chat_json_validated)
    out = metadata.llm_extract("some long enough first page text " * 5)
    assert out is not None
    assert [a["name"] for a in out["authors"]] == ["Anna Author"], "空名作者过滤 + 去空白"
    assert out["year"] == 2017


def test_llm_extract_none_on_short_text():
    assert metadata.llm_extract("short") is None


# ---------- 第 1 层：arXiv API（mock 网络） ----------

def test_fetch_arxiv_meta_invalid_id():
    assert metadata.fetch_arxiv_meta("not-an-id") is None


def test_fetch_arxiv_meta_network_failure(monkeypatch):
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(metadata.httpx, "get", boom)
    assert metadata.fetch_arxiv_meta("1706.03762") is None, "网络故障降级返回 None"


def test_fetch_arxiv_meta_parses_atom(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            return None

        text = """<feed xmlns="http://www.w3.org/2005/Atom"
                   xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry>
            <title>Attention Is All You Need</title>
            <summary>We propose the Transformer.  </summary>
            <author><name>Ashish Vaswani</name></author>
            <author><name>Bob Chen</name><arxiv:affiliation>Google Brain</arxiv:affiliation></author>
            <published>2017-06-12T17:57:34Z</published>
            <arxiv:doi>10.5555/3295222</arxiv:doi>
            <category term="cs.CL"/>
          </entry>
        </feed>"""

    monkeypatch.setattr(metadata.httpx, "get", lambda *a, **kw: FakeResp())
    out = metadata.fetch_arxiv_meta("1706.03762")
    assert out and out["year"] == 2017 and out["doi"] == "10.5555/3295222"
    assert out["authors"][0]["name"] == "Ashish Vaswani"
    assert out["authors"][0]["affiliation"] == "", "Atom 无机构 → 留空交 LLM 层补"
    assert out["authors"][1]["affiliation"] == "Google Brain"


# ---------- 编排：抽取 + 落库 + 缓存 ----------

def _paper_with_docir(tag: str) -> tuple[str, dict]:
    import uuid

    from app.config import DOCIR_DIR

    pid = f"p_meta_{uuid.uuid4().hex[:8]}"
    with db.tx() as c:
        c.execute("INSERT INTO papers(id,title,file_path,file_hash,created_at,meta_json)"
                  " VALUES(?,?,?,?,?,?)",
                  (pid, "Attention Is All You Need", "x.pdf", f"h_{pid}", db.now(), "{}"))
    docir = _docir_with_abstract()
    docir["meta"]["arxiv_id"] = "1706.03762v1"
    DOCIR_DIR.mkdir(parents=True, exist_ok=True)
    (DOCIR_DIR / f"{pid}.json").write_text(json.dumps(docir), encoding="utf-8")
    return pid, docir


def test_extract_merges_api_layer_and_updates_papers(monkeypatch):
    pid, docir = _paper_with_docir("m1")
    monkeypatch.setattr(
        metadata, "fetch_arxiv_meta", lambda base, timeout=8.0: {
            "authors": [{"name": "Ashish Vaswani", "affiliation": ""},
                        {"name": "Bob Chen", "affiliation": ""}],
            "abstract": "API abstract text.", "year": 2017, "doi": "10.5555/x",
            "journal_ref": "NeurIPS 2017", "categories": ["cs.CL"]})
    monkeypatch.setattr(metadata, "llm_extract", lambda text: {
        "authors": [{"name": "Ashish Vaswani", "affiliation": "Google Brain"},
                    {"name": "Bob Chen", "affiliation": "Google Brain"}],
        "abstract": "", "venue": "", "year": 0, "doi": ""})

    meta = metadata.extract_and_store_metadata(pid, docir)
    assert meta["abstract"] == "API abstract text.", "API 摘要优先"
    assert meta["year"] == 2017 and meta["venue"] == "NeurIPS 2017"
    assert meta["authors"][0]["affiliation"] == "Google Brain", "LLM 只补机构"
    assert meta["meta_source"] == "arxiv_api+llm"
    row = db.query("SELECT abstract, authors_json, meta_source FROM papers WHERE id=?",
                   (pid,))[0]
    assert row["abstract"] == "API abstract text."
    assert json.loads(row["authors_json"])[1]["name"] == "Bob Chen"
    assert row["meta_source"] == "arxiv_api+llm"


def test_extract_falls_back_heuristic_when_all_offline(monkeypatch):
    pid, docir = _paper_with_docir("m2")
    monkeypatch.setattr(metadata, "fetch_arxiv_meta", lambda base, timeout=8.0: None)
    monkeypatch.setattr(metadata, "llm_extract", lambda text: None)
    meta = metadata.extract_and_store_metadata(pid, docir)
    assert "Transformer" in meta["abstract"], "全降级 → 启发式摘要接管"
    assert meta["meta_source"] == "heuristic"


def test_arxiv_cache_prevents_second_fetch(monkeypatch):
    pid, docir = _paper_with_docir("m3")
    calls = {"n": 0}

    def fake_fetch(base, timeout=8.0):
        calls["n"] += 1
        return {"authors": [{"name": "A", "affiliation": ""}], "abstract": "cached abstract.",
                "year": 2017, "doi": "", "journal_ref": "", "categories": []}

    monkeypatch.setattr(metadata, "fetch_arxiv_meta", fake_fetch)
    monkeypatch.setattr(metadata, "llm_extract", lambda text: None)
    metadata.extract_and_store_metadata(pid, docir)
    assert calls["n"] == 1
    # 第二次：即使 API 变为不可达也走缓存
    monkeypatch.setattr(metadata, "fetch_arxiv_meta",
                        lambda base, timeout=8.0: (_ for _ in ()).throw(AssertionError("不该再请求")))
    meta2 = metadata.extract_and_store_metadata(pid, docir)
    assert meta2["abstract"] == "cached abstract."
