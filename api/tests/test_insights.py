"""4.6 图谱洞察测试（密闭）：enrich 角色/社区 / 含金量规则分类 / Future Work 去重 / 端点。"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SCHOLAR_DATA_DIR", str(Path(__file__).parent / f"_ins_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_PROVIDER"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["MINIMAX_API_KEY"] = ""
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["MINERU_ENABLED"] = "off"
os.environ["EMBEDDING_BACKEND"] = "off"

from app import db
from app.agents import insights
from app.agents.insights import _percentile, _rule_class, _year_of, classify_citations, enrich
from app.agents.tools import _citation_graph
from app.llm import client as llm
from app.llm import prompts
from app.rag.cjk import cjk_bigram

DATA = Path(os.environ["SCHOLAR_DATA_DIR"])

# A(2017 Transformer) ← B(2018) ← D(2024)；C(2020) ← A、D
PAPERS = {
    "pA": {"title": "IG-Attention Core", "arxiv_id": "1706.03762v7", "year": 2017},
    "pB": {"title": "Universal Transformers", "arxiv_id": "1801.00001v1", "year": 2018},
    "pC": {"title": "Longformer", "arxiv_id": "2001.00001v1", "year": 2020},
    "pD": {"title": "Mamba survey", "arxiv_id": "2401.00001v1", "year": 2024},
}
REFS = [   # (src, idx, target_title, target_arxiv, target_pid)
    ("pB", 1, "IG-Attention Core", "", "pA"),
    ("pC", 1, "IG-Attention Core", "", "pA"),
    ("pD", 1, "IG-Attention Core", "", "pA"),
    ("pD", 2, "Longformer", "2001.00001", "pC"),
    ("pD", 3, "Some External Paper", "", None),   # 外部引用
]

_DOCIR = {
    "pA": {"blocks": [
        {"id": "a1", "type": "paragraph", "text": "The attention mechanism [1] enables parallel training.",
         "page": 0, "section_path": "1 Introduction"},
        {"id": "a2", "type": "paragraph", "text": "We stack attention layers [1] with residual connections and it improves BLEU by 2.0.",
         "page": 1, "section_path": "3 Model"},
        {"id": "a3", "type": "paragraph",
         "text": "In future work we will explore sparse attention. Future direction: multi-modal attention.",
         "page": 9, "section_path": "5 Conclusion"},
    ]},
    "pB": {"blocks": [
        {"id": "b1", "type": "paragraph", "text": "We recast as universal transformer [1].",
         "page": 0, "section_path": "1 Introduction"},
    ]},
    "pC": {"blocks": [
        {"id": "c1", "type": "paragraph", "text": "Extends attention [1] to long documents via locality.",
         "page": 0, "section_path": "2 Method"},
    ]},
    "pD": {"blocks": [
        {"id": "d1", "type": "paragraph", "text": "Survey of attention successors [1] [2].",
         "page": 0, "section_path": "1 Introduction"},
        {"id": "d2", "type": "paragraph",
         "text": "Further research could unify SSM and attention models.",
         "page": 12, "section_path": "6 Limitations"},
    ]},
}


@pytest.fixture(scope="module", autouse=True)
def _seed():
    db.init_db()
    with db.tx() as c:
        for pid, p in PAPERS.items():
            c.execute("INSERT OR REPLACE INTO papers(id,title,file_path,file_hash,arxiv_id,"
                      "created_at,meta_json) VALUES(?,?,?,?,?,?,?)",
                      (pid, p["title"], "", f"h_{pid}", p["arxiv_id"], time.time(), "{}"))
        for i, (src, idx, t, aid, tgt) in enumerate(REFS):
            c.execute('INSERT OR REPLACE INTO "references"(id,paper_id,idx,raw,title,arxiv_id)'
                      " VALUES(?,?,?,?,?,?)", (f"r_{src}_{i}", src, idx, t, t, aid))
    from app.config import DOCIR_DIR
    DOCIR_DIR.mkdir(parents=True, exist_ok=True)
    for pid, d in _DOCIR.items():
        (Path(DOCIR_DIR) / f"{pid}.json").write_text(json.dumps(d), encoding="utf-8")
    # pA 一个可检索 chunk（综述证据链）
    text = "The attention mechanism is the main contribution of this paper."
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO chunks(id,paper_id,seq,text,text_cjk,page,bbox_json,"
                  "section_path,block_ids,kind) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  ("ck_a1", "pA", 0, text, cjk_bigram(text), 1, "[]", "3 Model", '["a2"]', "detail"))
        c.execute("INSERT OR REPLACE INTO chunks_fts(chunk_id,paper_id,text_cjk) VALUES(?,?,?)",
                  ("ck_a1", "pA", cjk_bigram(text)))
    prompts.reload()
    yield


# ---------- 单元 ----------

def test_year_and_rules():
    assert _year_of({"arxiv_id": "1706.03762v7"}) == 2017
    assert _year_of({"arxiv_id": "2401.00001"}) == 2024
    assert _year_of({}) is None
    assert _rule_class("we use it [1] in training", "3 Model") == "substantive"
    assert _rule_class("mentioned [1] as prior", "1 Introduction") == "background"
    assert _rule_class("misc [1]", "Appendix") == "perfunctory"
    assert _percentile([1, 2, 3, 4, 5], 0.7) == 3   # int(q*(n-1)) 取位约定，阈值一致性即可


# ---------- enrich：社区 / 角色 ----------

def test_enrich_graph_structure_and_roles():
    g = enrich(_citation_graph(list(PAPERS)))
    assert g["edge_count"] == 4                      # 外部引用不成边
    assert g["external_count"] == 1
    by_id = {n["paper_id"]: n for n in g["nodes"]}
    # A：2017 最早 + 3 入边（唯一高被引）→ 基石论文
    assert by_id["pA"]["role"] == "cornerstone"
    assert by_id["pA"]["in_degree"] == 3
    assert by_id["pD"]["year"] == 2024
    assert g["communities"] and all(c["size"] > 0 for c in g["communities"])
    assert isinstance(g["modularity"], float)


def test_classify_citations_rules_and_weights():
    w = classify_citations(["pA", "pB", "pC", "pD"])
    # pA 的 [1]：Introduction（背景 0.4）+ Model（实质 1.0）→ 边 pA→pA 不存在（自环），
    # 但 pA 自己引用 [1] 的目标是自己 → 不进 edge_weights；验证分类统计
    assert w["class_stat"]["substantive"] >= 2       # pA Model 句 + pC Method 句
    assert w["class_stat"]["background"] >= 2        # pA Intro 句 + pB Intro 句
    # 边权重：pB→pA 引用 [1]（Introduction → 背景 0.4）
    keys = {f"{e['source']}|{e['target']}" for e in w["edges"]}
    assert "pB|pA" in keys
    edge_b = next(e for e in w["edges"] if e["source"] == "pB" and e["target"] == "pA")
    assert edge_b["weight"] == pytest.approx(0.4)
    # pD→pA：Intro 句（背景 0.4）
    edge_d = next(e for e in w["edges"] if e["source"] == "pD" and e["target"] == "pA")
    assert edge_d["weight"] == pytest.approx(0.4)


def test_community_summary_with_stub(monkeypatch):
    calls = {"n": 0}

    def stub_chat(msgs, **kw):
        calls["n"] += 1
        if calls["n"] == 1:      # 综述生成
            return {"text": "奠基期由 Attention 确立注意力机制 [E1]。", "model": "stub"}
        return {"text": json.dumps({"claims": [{"text": "奠基期确立注意力 [E1]",
                                                "evidence_ids": [1],
                                                "quote": "The attention mechanism"}]},
                                   ensure_ascii=False), "model": "stub"}

    monkeypatch.setattr(llm, "available", lambda force=False: True)
    monkeypatch.setattr(llm, "chat", stub_chat)
    out = insights.community_summary(list(PAPERS), 0)
    assert "summary" in out and "[E1]" in out["summary"]
    assert "claims" in out and "anchors" in out
    if out["claims"]:                               # ClaimTrace 走通时三态齐全
        assert out["claims"][0]["status"] in ("supported", "needs_check", "unsupported")


def test_community_summary_no_llm(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda force=False: False)
    out = insights.community_summary(list(PAPERS), 0)
    assert "llm_unavailable_fallback" in out["guardrails"]


# ---------- Future Work 去重过滤 ----------

def test_future_works_no_llm(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda force=False: False)
    out = insights.future_works(["pA", "pD"])
    assert out["total"] >= 2
    # pA：无后继（没人引用 pA？pB/pC/pD 都引用了 pA！）→ pA 的想法有后继
    # pD：无人引用 pD → open + 缝隙理由
    d_ideas = [x for x in out["open"] if x["source_paper"] == "pD"]
    assert d_ideas and any("缝隙" in x["reason"] for x in d_ideas)
    # pA 有后继（pB/pC/pD 引用）→ 走判定通道，无 LLM 时保守 open
    a_ideas = [x for x in out["open"] + out["solved"] if x["source_paper"] == "pA"]
    assert a_ideas, "pA 的 future work 应被抽出"
    assert all(x["status"] == "open" for x in a_ideas)   # 无 LLM → 保守 open


def test_future_works_judge_solved(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda force=False: True)

    def stub_chat(msgs, **kw):
        if "你是研究缝隙判定器" in msgs[0]["content"]:
            vid = 0
            return {"text": json.dumps({"verdicts": [{"id": vid, "verdict": "solved",
                                                      "reason": "pB 已实现"}]}), "model": "stub"}
        if "你是研究机会抽取器" in msgs[0]["content"]:
            return {"text": json.dumps({"ideas": [{"idea_text": "探索稀疏注意力"}]}), "model": "stub"}
        return {"text": "", "model": "stub"}

    monkeypatch.setattr(llm, "chat", stub_chat)
    out = insights.future_works(["pA"])
    assert out["total"] >= 1
    assert all(x["status"] in ("solved", "partial", "open") for x in out["solved"] + out["open"])


# ---------- 端点 ----------

def test_graph_endpoint():
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)
    # 显式 paper_ids（密闭：进程内 data 目录已由 conftest 会话级隔离，此处仍显式圈定）
    r = client.get("/api/graph", params={"paper_ids": "pA,pB,pC,pD"})
    assert r.status_code == 200
    g = r.json()
    assert g["edge_count"] == 4
    assert any(n["role"] == "cornerstone" for n in g["nodes"])
    assert all("weight" in e for e in g["edges"])        # 加权边
    assert g["insights"]["cornerstone"]


def test_ideas_endpoint_sse(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    monkeypatch.setattr(llm, "available", lambda force=False: False)
    client = TestClient(app)
    events = []
    with client.stream("POST", "/api/ideas", json={"paper_ids": ["pA", "pD"]}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("event: "):
                events.append(line[7:].strip())
    assert "status" in events and "final" in events and "done" in events


# ---------- 模块五 P0-2/P0-3：整批综述 + 外部进展核查 ----------

def test_survey_skeleton_without_llm(monkeypatch):
    """LLM 不可用 → 图谱客观数据骨架如实交付（不冒充综述正文）。"""
    from app.agents import insights as ins
    monkeypatch.setattr(ins.llm, "available", lambda: False)
    out = ins.survey(["pA", "pB"])
    assert "error" not in out
    assert [s["title"] for s in out["sections"]] == ins._SURVEY_SECTIONS
    assert "图谱客观数据" in out["content_md"]
    assert "llm_unavailable_skeleton" in out["guardrails"]
    assert ins.survey(["only-one"])["error"] == "survey_needs_2_papers"


def test_survey_sections_with_stub_llm(monkeypatch):
    from app.agents import insights as ins
    monkeypatch.setattr(ins.llm, "available", lambda: True)
    monkeypatch.setattr(ins.llm, "chat", lambda msgs, **kw: {
        "text": "本节内容 [E1] 支撑。"})
    monkeypatch.setattr(ins, "_evidence_pack", lambda pids: ([{"paper_id": "pA"}],
                                                              {"context": "ctx",
                                                               "eid_map": {1: {"paper_id": "pA"}},
                                                               "titles": {}}))
    monkeypatch.setattr(ins, "validate_citations", lambda a, m: (a, []))
    monkeypatch.setattr(ins, "llm_claims_safe", lambda a, h: ([], {}))
    monkeypatch.setattr(ins, "verify_claims", lambda c, m: (c, []))
    import app.rag.answering as _ans
    monkeypatch.setattr(_ans, "build_anchors", lambda m: {})
    monkeypatch.setattr(ins, "_citation_graph", lambda pids: {"nodes": [], "edges": [], "external_count": 0})
    monkeypatch.setattr(ins, "enrich", lambda g: g)
    out = ins.survey(["pA", "pB"])
    assert len(out["sections"]) == 5 and "[E1]" in out["sections"][0]["content"]
    assert "content_md" in out and "## 背景" in out["content_md"]


def test_dedup_ideas_merges_similar():
    from app.agents import insights as ins
    ideas = [
        {"idea_text": "Study scaling laws for sparse attention models in future",
         "source_paper": "p1"},
        {"idea_text": "scaling laws sparse attention models future work",
         "source_paper": "p2"},
        {"idea_text": "Improve OCR for handwritten Chinese medical records",
         "source_paper": "p1"},
    ]
    got = ins._dedup_ideas(ideas)
    assert len(got) == 2, "前两条应合并（Jaccard ≥0.55）"
    assert set(got[0]["mention_papers"]) == {"p1", "p2"}


def test_future_check_no_evidence_open_unverified(monkeypatch):
    """外部源失败 → open_unverified（不冒充已排除），不写死 open。"""
    from app.agents import insights as ins
    monkeypatch.setattr(ins, "_extract_future_ideas", lambda pids: [
        {"idea_text": "Explore long-context retrieval augmentation", "source_paper": "p1"}])
    from app.rag import discovery as _disc
    monkeypatch.setattr(_disc, "_s2_provider", lambda q, l: {"ok": False, "entries": []})
    got = ins.future_checks(["p1"])
    assert len(got) == 1 and got[0]["verdict"] == "open_unverified"
    assert "未验证" in got[0]["reason"]


def test_future_check_with_evidence_llm_judge(monkeypatch):
    from app.agents import insights as ins
    monkeypatch.setattr(ins, "_extract_future_ideas", lambda pids: [
        {"idea_text": "Explore long-context retrieval augmentation", "source_paper": "p1"}])
    from app.rag import discovery as _disc
    monkeypatch.setattr(_disc, "_s2_provider", lambda q, l: {"ok": True, "entries": [
        {"title": "Long-Context RAG at Scale", "year": 2026, "citation_count": 42,
         "abstract": "We fully implement long-context retrieval augmentation."}]})

    class _FakeData:
        pass

    monkeypatch.setattr(ins.llm, "available", lambda: True)
    monkeypatch.setattr(ins.llm, "chat_json_validated",
                        lambda msgs, schema, **kw: {"checks": [
                            {"id": 0, "verdict": "solved", "confidence": 0.9,
                             "reason": "Long-Context RAG at Scale (2026) 已实现"}]})
    got = ins.future_checks(["p1"])
    assert got[0]["verdict"] == "solved" and got[0]["confidence"] == 0.9
    assert got[0]["external"][0]["title"].startswith("Long-Context")
