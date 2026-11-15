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
    "pA": {"title": "IG-Attention Core", "arxiv_id": "1706.99999v1", "year": 2017},
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
    for _name in ("_s2_provider", "_openalex_provider", "_arxiv_provider"):
        # W5 起外部证据链 S2 → OpenAlex → arXiv，三源都需封闭（否则真实网络请求）
        monkeypatch.setattr(_disc, _name, lambda q, l: {"ok": False, "entries": []})
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


# ---------- Round4：20 篇阈值校准 + ±10% 扰动稳定性 ----------

def _seed_20_papers() -> list[str]:
    """20 篇合成论文：3 篇 2015-2016 早期枢纽 + 13 篇 2017-2022 中坚 + 4 篇 2024 新作。"""
    pids = []
    with db.tx() as c:
        for i in range(20):
            pid = f"p_g20_{i:02d}"
            year = 2015 + i // 2 + (2 if i >= 16 else 0)   # 2015..2022 + 2024 尾段
            aid = f"{year % 100:02d}01.{10000 + i}"
            c.execute("INSERT OR REPLACE INTO papers(id,title,arxiv_id,status,file_path,"
                      "file_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                      (pid, f"Synthetic Transformer Study {i}", f"{aid}v1", "active", "", f"h{pid}", 0.0))
            pids.append(pid)
    return pids


def _build_g20_edges(pids: list[str]) -> list[dict]:
    """引用结构：3 个早期枢纽被 12 篇引用（高 in_degree）；4 篇 2024 新作各引 1 个
    不同枢纽（跨社区桥接、低被引 → 边缘衍生候选）。"""
    hubs = pids[:3]
    mids = pids[3:15]
    news = pids[16:20]
    edges = []
    for i, m in enumerate(mids):
        edges.append({"source": m, "target": hubs[i % 3]})
    for j, nw in enumerate(news):
        edges.append({"source": nw, "target": hubs[j % 3]})   # 每篇连不同枢纽=跨社区
        edges.append({"source": nw, "target": mids[j]})       # 加一条避免全孤点
    return edges


def test_role_threshold_calibration_20_papers():
    """20 篇下角色判据应产出分化（非全空/非全基石），且 ±10% 扰动下基石集合稳定。"""
    pids = _seed_20_papers()
    try:
        nodes = [{"id": p, "title": f"Synthetic Transformer Study {i}"}
                  for i, p in enumerate(pids)]
        edges = _build_g20_edges(pids)
        g = enrich({"nodes": nodes, "edges": edges, "external_count": 0})
        roles = [n.get("role", "") for n in g["nodes"]]
        n_corner = roles.count("cornerstone")
        n_periph = roles.count("peripheral")
        assert 0 < n_corner < len(pids), f"基石应分化而非全空/全基石（实际 {n_corner}）"
        assert all(n.get("role_reason") for n in g["nodes"] if n.get("role")), "角色必须带可解释理由"

        # ±10% 扰动：增/删 10% 引用边（19 条中 ±2 条）→ 基石集合成员应基本保持
        def corner_set(eds):
            gg = enrich({"nodes": nodes, "edges": eds, "external_count": 0})
            return {n["paper_id"] if "paper_id" in n else n["id"]
                    for n in gg["nodes"] if n.get("role") == "cornerstone"}

        base = corner_set(edges)
        perturbed_up = edges + [{"source": pids[10], "target": pids[0]},
                                {"source": pids[11], "target": pids[1]}]
        perturbed_dn = [e for k, e in enumerate(edges) if k not in (0, 1)]
        up = corner_set(perturbed_up)
        dn = corner_set(perturbed_dn)
        assert len(up & base) >= len(base) - 1, "+10% 扰动基石集合应稳定"
        assert len(dn & base) >= len(base) - 1, "-10% 扰动基石集合应稳定"
        assert up and dn, "扰动后角色判定不应全体塌缩为空"
        _ = n_periph   # 边缘角色在本合成结构下可为 0（跨社区判据较严），不设硬门禁
    finally:
        with db.tx() as c:
            c.execute("DELETE FROM papers WHERE id LIKE 'p_g20_%'")


# ---------- Round7 W4：三种引用样式解析 + 含金量覆盖 ----------

def test_classify_citations_author_year_keyed_grouped(monkeypatch):
    """author-year / 字母键 / 分组数字三种真实语料样式都解析成 references idx。"""
    from app.agents import insights as ins

    refs = [
        {"idx": 1, "title": "BERT: Pre-training of Deep Bidirectional Transformers",
         "arxiv_id": "1810.04805",
         "raw": "Jacob Devlin, Ming-Wei Chang, Kenton Lee, and Kristina Toutanova. 2019. BERT...",
         "authors": "Jacob Devlin, Ming-Wei Chang, Kenton Lee, and Kristina Toutanova",
         "year": 2019},
        {"idx": 2, "title": "The PASCAL Recognising Textual Entailment Challenge",
         "arxiv_id": "",
         "raw": "[DGM06] Ido Dagan, Oren Glickman, and Bernardo Magnini. The PASCAL...",
         "authors": "[DGM06] Ido Dagan, Oren Glickman, and Bernardo Magnini", "year": 2006},
        {"idx": 13, "title": "Prior Sparse Work A", "arxiv_id": "",
         "raw": "Some Author. 2018. Prior sparse work A.", "authors": "Some Author",
         "year": 2018},
        {"idx": 14, "title": "Prior Sparse Work B", "arxiv_id": "",
         "raw": "Other Author. 2019. Prior sparse work B.", "authors": "Other Author",
         "year": 2019},
        {"idx": 15, "title": "Deep contextualized word representations", "arxiv_id": "",
         "raw": "Matthew E. Peters, Mark Neumann, James Iyyer, and Luke Zettlemoyer. 2018a. "
                "Deep contextualized word representations.",
         "authors": "Matthew E. Peters, Mark Neumann, James Iyyer, and Luke Zettlemoyer",
         "year": 2018},
    ]
    papers = [{"id": "pX", "title": "Author Year Paper", "arxiv_id": "1901.00001"}]

    def fake_query(sql, params=()):
        if "FROM papers" in sql:
            return papers
        if 'FROM "references"' in sql:
            return refs
        raise AssertionError(f"unexpected SQL: {sql[:60]}")

    doc = {"blocks": [
        {"type": "paragraph", "section_path": "3 Model",
         "text": "We build on BERT (Devlin et al., 2019) and GPT-3 [DGM06]. "
                 "Prior work is sparse [13, 14]."},
        {"type": "paragraph", "section_path": "1 Introduction",
         "text": "Peters et al. (2018a) introduced contextual embeddings."},
    ]}
    monkeypatch.setattr(ins.db, "query", fake_query)
    monkeypatch.setattr(ins, "_load_docir", lambda pid: doc)
    monkeypatch.setattr(ins.llm, "available", lambda force=False: False)

    r = ins.classify_citations(["pX"], graph={"nodes": [], "edges": [],
                                              "external_count": 0})
    keys = set(r["enriched"])
    assert {"pX|1", "pX|2", "pX|13", "pX|14", "pX|15"} <= keys
    assert r["mention_count"] == 5, "author-year 1 + 字母键 1 + 分组数字 2 + 叙述式 1"
    assert sum(r["class_stat"].values()) == r["mention_count"]
    assert r["class_stat"]["substantive"] == 4 and r["class_stat"]["background"] == 1


def test_classify_citations_llm_batches_fail_isolated(monkeypatch):
    """W4：LLM 增强分批（40/批）；某批失败只记 guardrail 并规则兜底，不影响整体。"""
    from app.agents import insights as ins

    refs = [{"idx": 1, "title": "Target Ref", "arxiv_id": "",
             "raw": "Some Author. 2018. Target ref.", "authors": "Some Author", "year": 2018}]
    papers = [{"id": "pY", "title": "Batch Paper", "arxiv_id": "1901.00002"}]

    def fake_query(sql, params=()):
        if "FROM papers" in sql:
            return papers
        if 'FROM "references"' in sql:
            return refs
        raise AssertionError(sql[:60])

    doc = {"blocks": [{"type": "paragraph", "section_path": "3 Model",
                       "text": " ".join(f"We use it [1] in step {k}." for k in range(41))}]}
    calls = {"n": 0}

    def stub_json(msgs, schema, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("batch1 timeout")
        return {"classes": [{"idx": 40, "cls": "background"}]}

    monkeypatch.setattr(ins.db, "query", fake_query)
    monkeypatch.setattr(ins, "_load_docir", lambda pid: doc)
    monkeypatch.setattr(ins.llm, "available", lambda force=False: True)
    monkeypatch.setattr(ins.llm, "chat_json_validated", stub_json)
    r = ins.classify_citations(["pY"], graph={"nodes": [], "edges": [],
                                              "external_count": 0})
    assert r["mention_count"] == 41
    assert "cite_llm_batch_1_rule_fallback" in r["guardrails"]
    assert r["class_stat"]["background"] == 1, "第二批 LLM 分类生效"
    assert r["class_stat"]["substantive"] == 40, "第一批失败 → 规则兜底"


def test_reference_quality_exposes_mention_count_and_coverage():
    """W4：ref-quality 返回 mention_count / coverage（有引用句条目/总 refs），恒 ≤1。"""
    from app.agents import insights as ins
    rq = ins.reference_quality("pB")       # pB 的 docir：1 句引用 [1]，references 1 条
    assert rq["mention_count"] >= 1
    assert rq["class_stat"] and sum(rq["class_stat"].values()) == rq["mention_count"]
    assert 0.0 <= rq["coverage"] <= 1.0
    assert rq["covered_refs"] <= rq["total_refs"] == 1
    assert "note" in rq and "中性态" in rq["note"]


def test_year_of_prefers_arxiv_then_pub_year():
    """_year_of：arXiv YYMM → pub_year（1900..当前年+1）→ created_at。"""
    assert _year_of({"arxiv_id": "1706.03762v7"}) == 2017      # Attention
    assert _year_of({"arxiv_id": "2005.14165"}) == 2020        # GPT-3
    assert _year_of({"pub_year": 2023, "created_at": 1794159018.7}) == 2023  # UDL 教材
    # pub_year 越界（下载入库年 2026 之类）→ 回退 created_at，不冒充发表年
    assert _year_of({"pub_year": 2999, "created_at": 1700000000.0}) == 2023


# ---------- Round7 W5：Future Work / 时间线 ----------

def test_dedup_ideas_merges_chinese_synonyms_and_keeps_earliest_year():
    from app.agents import insights as ins
    ideas = [
        {"idea_text": "未来工作将探索稀疏注意力机制", "source_paper": "p1", "source_year": 2021},
        {"idea_text": "未来工作探索稀疏注意力机制", "source_paper": "p2", "source_year": 2019},
        {"idea_text": "!!! ???", "source_paper": "p1", "source_year": 2022},
        {"idea_text": "%%% $$$", "source_paper": "p2"},
    ]
    got = ins._dedup_ideas(ideas)
    assert len(got) == 3, "中文近义合并；空词元集两条不参与匹配、各自保留"
    assert set(got[0]["mention_papers"]) == {"p1", "p2"}
    assert got[0]["source_year"] == 2019, "合并时 source_year 取较早者"


def test_extract_future_ideas_tail_heading_filters_noise(monkeypatch):
    """按 heading 定位 Conclusion 后续段落；过滤附录指路/引用行；每条带 source_year。"""
    from app.agents import insights as ins

    doc = {"blocks": [
        {"type": "heading", "text": "4 Experiments", "section_path": ""},
        {"type": "paragraph", "section_path": "4 Experiments",
         "text": "We run experiments. Future work in a non-tail section is out of scope."},
        {"type": "heading", "text": "5 Conclusion", "section_path": ""},
        {"type": "paragraph", "section_path": "5 Conclusion",
         "text": "In future work, we plan to explore sparse attention. "
                 "For more formal analysis, please refer to Appendix A. "
                 "Future research could study multimodal attention."},
        {"type": "heading", "text": "References", "section_path": ""},
        {"type": "paragraph", "section_path": "References",
         "text": "[1] Some Author. Future work inside reference lines should not count. "
                 "2017. arXiv preprint arXiv:1706.03762."},
    ]}
    monkeypatch.setattr(ins, "_load_docir", lambda pid: doc)
    monkeypatch.setattr(ins.llm, "available", lambda force=False: False)

    ideas = ins._extract_future_ideas(["pA"])       # 测试库 pA: arXiv 1706.99999 → 2017
    texts = [i["idea_text"] for i in ideas]
    assert any("explore sparse attention" in t for t in texts)
    assert any("multimodal attention" in t for t in texts)
    assert not any("please refer" in t.lower() or "formal analysis" in t.lower()
                   for t in texts), "附录指路噪声句必须被过滤"
    assert not any("reference lines" in t.lower() for t in texts)
    assert not any("non-tail section" in t.lower() for t in texts), "只扫尾章后续段落"
    assert ideas and all(i["source_year"] == 2017 for i in ideas)


def test_future_checks_rule_partial_without_llm(monkeypatch):
    """S2 失败 → OpenAlex 兜底；LLM 不可用 → 规则初判 partial/0.4 + verdict_source=rule。"""
    from app.agents import insights as ins
    from app.rag import discovery as disc

    monkeypatch.setattr(ins, "_extract_future_ideas", lambda pids: [
        {"idea_text": "long-context retrieval augmentation for scientific documents",
         "source_paper": "p1", "source_year": 2020}])
    monkeypatch.setattr(ins.llm, "available", lambda force=False: False)
    monkeypatch.setattr(disc, "_s2_provider", lambda q, l: {"ok": False, "entries": []})
    monkeypatch.setattr(disc, "_openalex_provider", lambda q, l: {"ok": True, "entries": [
        {"title": "Long-Context Retrieval Augmentation for Scientific Documents",
         "year": 2024, "citation_count": 12,
         "abstract": "We study long-context retrieval augmentation for scientific documents."}]})
    monkeypatch.setattr(disc, "_arxiv_provider", lambda q, l: {"ok": False, "entries": []})

    got = ins.future_checks(["p1"])
    assert len(got) == 1
    it = got[0]
    assert it["verdict"] == "partial" and it["confidence"] == 0.4
    assert it["verdict_source"] == "rule" and "规则初判" in it["reason"]
    assert it["external"] and it["external"][0]["source"] == "openalex"
    assert it["external"][0]["verdict_source"] == "rule", "verdict_source 进 evidence JSON"
    assert it["external"][0]["year"] == 2024


def test_future_checks_no_evidence_all_open_unverified(monkeypatch):
    """三源全部失败/无命中 → 全 open_unverified（不冒充已排除）。"""
    from app.agents import insights as ins
    from app.rag import discovery as disc

    monkeypatch.setattr(ins, "_extract_future_ideas", lambda pids: [
        {"idea_text": "Explore long-context retrieval augmentation", "source_paper": "p1"}])
    monkeypatch.setattr(ins.llm, "available", lambda force=False: False)
    for name in ("_s2_provider", "_openalex_provider", "_arxiv_provider"):
        monkeypatch.setattr(disc, name, lambda q, l: {"ok": False, "entries": []})

    got = ins.future_checks(["p1"])
    assert len(got) == 1
    assert got[0]["verdict"] == "open_unverified" and got[0]["confidence"] == 0.0
    assert got[0]["external"] == [] and "未验证" in got[0]["reason"]


def test_future_checks_year_filter_and_rule_miss_stays_open(monkeypatch):
    """只保留 year >= source_year；规则重叠不足 → 保持 open_unverified 且证据/原因齐全。"""
    from app.agents import insights as ins
    from app.rag import discovery as disc

    monkeypatch.setattr(ins, "_extract_future_ideas", lambda pids: [
        {"idea_text": "sparse attention kernel optimization", "source_paper": "p1",
         "source_year": 2020}])
    monkeypatch.setattr(ins.llm, "available", lambda force=False: False)
    monkeypatch.setattr(disc, "_s2_provider", lambda q, l: {"ok": True, "entries": [
        {"title": "Earlier Work From 2018", "year": 2018, "citation_count": 5,
         "abstract": "sparse attention kernel optimization"}]})
    monkeypatch.setattr(disc, "_openalex_provider", lambda q, l: {"ok": True, "entries": [
        {"title": "Protein Folding With Diffusion Models", "year": 2024,
         "citation_count": 30,
         "abstract": "A study of protein structure prediction and diffusion models."}]})
    monkeypatch.setattr(disc, "_arxiv_provider", lambda q, l: {"ok": False, "entries": []})

    got = ins.future_checks(["p1"])
    it = got[0]
    assert it["verdict"] == "open_unverified"
    assert [e["title"] for e in it["external"]] == ["Protein Folding With Diffusion Models"], \
        "2018 < source_year 2020 的条目必须被过滤"
    assert it["external"][0]["source"] == "openalex"
    assert "未验证" in it["reason"] or "不冒充" in it["reason"]
