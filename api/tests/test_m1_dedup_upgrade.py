"""模块一 P0 升级回归：9 层方案新增能力——LSH blocking、boilerplate 滤除、标题归一 v2、
DOI 标识符层、族聚类（union-find + /api/families）、cross-encoder 降级、LLM 灰区复核。"""
from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

from app import db, jobs
from app.config import settings
from app.rag import fingerprint as fp


def _client() -> TestClient:
    from app.main import app

    return TestClient(app)


def _insert_paper(pid: str, title: str, *, arxiv: str = "", doi: str = "",
                  year: int = 0, family: str = "", abstract: str = "",
                  sig: bytes | None = None, authors: list | None = None,
                  version: str = "") -> None:
    """直插 papers 行（绕过解析管线，聚焦去重层单元行为）。"""
    with db.tx() as c:
        c.execute(
            "INSERT INTO papers(id,title,file_path,file_hash,arxiv_id,doi,pub_year,"
            "family_id,status,abstract,authors_json,content_sig,version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'active',?,?,?,?)",
            (pid, title, f"x/{pid}.pdf", uuid.uuid4().hex, arxiv, doi, year,
             family, abstract, json.dumps(authors or [], ensure_ascii=False),
             sig, version, db.now()))


def _row(pid: str):
    return db.query("SELECT * FROM papers WHERE id=?", (pid,))[0]


# ---------- L3：boilerplate 滤除 ----------

def test_strip_boilerplate():
    header = "International Conference on Very Important Research"
    blocks = [header, "1", "arXiv:2501.00001v1", "Real finding one about attention.",
              header, "2", "Real finding two about scaling.", header, "3",
              "Real finding three about data.", "References",
              "[1] Someone. 2020.", "[2] Other. 2021."]
    out = fp.strip_boilerplate(blocks)
    assert header not in out, "重复 ≥3 次的页眉块必须滤除"
    assert "References" not in out and "[1] Someone" not in out, "References 起截断"
    assert "arXiv" not in out and "finding one" in out
    assert fp.strip_boilerplate("a line\nanother line").count("line") == 2


# ---------- L3：标题归一 v2 + 双条件 title_sim ----------

def test_title_norm_v2_and_dual_condition():
    # NFKC：全角折叠后与半角同串
    assert fp.title_norm_v2("Ａｔｔｅｎｔｉｏｎ ｉｎｔｅｒｎａｌ") == fp.title_norm_v2("Attention internal")
    # 空白/大小写鲁棒
    assert fp.title_sim("Attention Is All You Need",
                        "attention  is  all  you   need") == 1.0
    # 副标题截断：主标题够长时冒号后丢弃
    assert fp.title_norm_v2("Deep Residual Learning: Identity Mappings") \
        == fp.title_norm_v2("Deep Residual Learning")
    # 截断后过短 → 保留全长
    assert fp.title_norm_v2("BERT: Notes") != fp.title_norm_v2("BERT")
    # 双条件：仅尾词异的短标题不再被字符比虚高（旧 max 规则给 ≈0.95）
    assert fp.title_sim("Semantic Dedup A", "Semantic Dedup B") < 0.8
    # 真同题
    assert fp.title_sim("Attention Is All You Need",
                        "Attention Is All You Need") >= 0.99


# ---------- L3：LSH banding ----------

def test_lsh_band_recall_and_specificity():
    # 每 10 个 token 换 1 个 → shingle 重合 ≈0.75（5-char shingle 随数字 token 稀疏分布，
    # 必须用「稀疏替换」而非「前缀共享」构造相似文本）
    text_a = " ".join(f"tok{i:04d}" for i in range(600))
    text_b = " ".join(f"alt{i:04d}" if i % 10 == 0 else f"tok{i:04d}" for i in range(600))
    sig_a, sig_b = fp.minhash_sig(text_a), fp.minhash_sig(text_b)
    assert fp.minhash_jaccard(sig_a, sig_b) > 0.55, "测试前提：高重合签名"
    assert set(fp.lsh_bands(sig_a)) & set(fp.lsh_bands(sig_b)), "高重合签名必须共享 band"
    text_c = " ".join(f"zzz{i:04d}" for i in range(600))
    sig_c = fp.minhash_sig(text_c)
    assert not (set(fp.lsh_bands(sig_a)) & set(fp.lsh_bands(sig_c))), "低重合不得命中 band"
    assert fp.lsh_bands(b"") == [] and fp.lsh_bands(None) == []


# ---------- L2：DOI 标识符精确层 ----------

def test_doi_identifier_floor_and_alert():
    _insert_paper("p_doi_a", "Totally Different Title Alpha", arxiv="2608.00001",
                  doi="https://doi.org/10.1234/Same-DOI", year=2024)
    _insert_paper("p_doi_b", "Another Unrelated Title Beta", doi="10.1234/same-doi",
                  year=2025)
    score, bd = jobs._score_pair(_row("p_doi_a"), _row("p_doi_b"))
    assert bd.get("same_doi") is True, "DOI 归一（去 URL 前缀/大小写）后必须判定同源"
    assert score >= 0.90, "DOI 同源享受确定性抬底"
    result = jobs.dedup_scan("p_doi_b")
    assert result and result["passed"], "DOI 同源必须触发预警"
    assert "DOI 同源" in result["reason"]


# ---------- L6：cross-encoder 缺失降级 ----------

def test_rerank_missing_normalization():
    _insert_paper("p_rr_a", "Gradient Compression for Large Models", abstract="")
    _insert_paper("p_rr_b", "Gradient Compression for Large Models", abstract="")
    budget = [5]
    score, bd = jobs._score_pair(_row("p_rr_a"), _row("p_rr_b"), rerank_budget=budget)
    assert "rerank" not in bd, "无 rerank key 时分量缺失（密闭环境无凭据）"
    assert score >= 0.85, "缺失归一化：标题 1.0 应把综合分抬满"
    assert budget[0] == 5, "rerank 不可用时不消耗额度"


# ---------- L7：LLM 灰区复核 ----------

_AUTHORS = [{"name": "Ada Lovelace"}, {"name": "Grace Hopper"}]


def test_llm_judge_veto_on_near_miss(monkeypatch):
    # 近失对 + 判异族 → 补警被否决（双向均无 pending）
    monkeypatch.setattr(settings, "dedup_threshold", 0.95)
    _insert_paper("p_jg_a", "Contrastive Objectives for Molecular Graphs QxVeto",
                  abstract="molecular contrastive learning with augmentations",
                  authors=_AUTHORS)
    _insert_paper("p_jg_b", "Contrastive Objectives with Molecular Graphs QxVeto",
                  abstract="molecular contrastive learning with augmentations",
                  authors=_AUTHORS)
    miss = jobs.dedup_scan("p_jg_b")
    assert miss and not miss["passed"] and miss["alert_id"] is None, "该对应为灰区近失"
    monkeypatch.setattr(jobs, "_llm_judge_pair",
                        lambda a, b: {"same_family": False, "confidence": 0.9,
                                      "reason": "研究工作不同"})
    assert jobs._refine_with_judge("p_jg_b", miss) is None, "判异族 → 近失对不补警"
    n = db.query("SELECT COUNT(*) c FROM dedup_alerts WHERE status='pending' AND "
                 "((new_paper_id='p_jg_a' AND existing_paper_id='p_jg_b') OR "
                 "(new_paper_id='p_jg_b' AND existing_paper_id='p_jg_a'))")[0]["c"]
    assert n == 0


def test_llm_judge_promotes_near_miss(monkeypatch):
    # 近失对 + 判同族 + 高 conf → 补警抬至阈值，breakdown 带 llm 分量
    monkeypatch.setattr(settings, "dedup_threshold", 0.95)
    _insert_paper("p_jg_c", "Contrastive Objectives for Molecular Graphs QxPromo",
                  abstract="molecular contrastive learning with augmentations",
                  authors=_AUTHORS)
    _insert_paper("p_jg_d", "Contrastive Objectives with Molecular Graphs QxPromo",
                  abstract="molecular contrastive learning with augmentations",
                  authors=_AUTHORS)
    miss = jobs.dedup_scan("p_jg_d")
    assert miss and not miss["passed"] and miss["alert_id"] is None
    monkeypatch.setattr(jobs, "_llm_judge_pair",
                        lambda a, b: {"same_family": True, "confidence": 0.9,
                                      "reason": "标题仅介词差异，摘要一致"})
    promoted = jobs._refine_with_judge("p_jg_d", miss)
    assert promoted and promoted["passed"], "复核同族 → 补警抬至阈值"
    assert promoted["breakdown"].get("llm") == 0.9
    assert "LLM 复核" in promoted["reason"]


def test_llm_judge_opinion_kept_on_id_floor(monkeypatch):
    # 确定性同源（same_arxiv 抬底）但正文指纹弱 → 复核后判异族也不 veto，
    # 保留预警并附 LLM 存疑意见（id 证据优先，人工最终裁决）
    _insert_paper("p_jg_e", "Weak Fingerprint Pair Paper", arxiv="2602.00001",
                  abstract="shared scaffolding sentence about pretraining",
                  authors=_AUTHORS, sig=fp.minhash_sig(
                      " ".join(f"tok{i:04d}" for i in range(600))))
    _insert_paper("p_jg_f", "Weak Fingerprint Pair Paper", arxiv="2602.00001",
                  abstract="shared scaffolding sentence about pretraining",
                  authors=_AUTHORS, sig=fp.minhash_sig(
                      " ".join(f"alt{i:04d}" for i in range(600))))
    result = jobs.dedup_scan("p_jg_f")
    assert result and result["passed"], "same_arxiv 抬底必须过阈"
    assert result["breakdown"].get("minhash", 1.0) < 0.5, "测试前提：正文指纹弱"
    monkeypatch.setattr(jobs, "_llm_judge_pair",
                        lambda a, b: {"same_family": False, "confidence": 0.85,
                                      "reason": "正文工作完全不同"})
    refined = jobs._refine_with_judge("p_jg_f", result)
    assert refined and refined["passed"], "id 同源不受 veto"
    assert "LLM 复核" in refined["reason"], "必须附 LLM 存疑意见"
    n = db.query("SELECT COUNT(*) c FROM dedup_alerts WHERE status='pending' AND "
                 "((new_paper_id='p_jg_e' AND existing_paper_id='p_jg_f') OR "
                 "(new_paper_id='p_jg_f' AND existing_paper_id='p_jg_e'))")[0]["c"]
    assert n == 2, "双向 pending 保留"


# ---------- L8：族聚类（union-find 传递闭包）+ /api/families ----------

def test_family_rebuild_transitive_and_api():
    _insert_paper("p_fa", "Family Alpha One", arxiv="2607.00001", year=2024, version="v1")
    _insert_paper("p_fb", "Family Alpha One", arxiv="2607.00001", year=2025, version="v2")
    _insert_paper("p_fc", "Family Alpha Extended Study", arxiv="", year=2026, version="")
    # B↔C 人工决策对（coexist 终态）→ 成为族边
    with db.tx() as c:
        c.execute("INSERT INTO dedup_alerts(id,new_paper_id,existing_paper_id,score,"
                  "breakdown_json,reason,status,created_at) VALUES(?,?,?,?,?,?,'coexisted',?)",
                  ("da_x1", "p_fb", "p_fc", 0.9, "{}", "决策", db.now()))
    jobs.rebuild_families()
    fams = {r["id"]: r["family_id"] for r in
            db.query("SELECT id, family_id FROM papers WHERE id IN ('p_fa','p_fb','p_fc')")}
    assert len(set(fams.values())) == 1, "A-B 同基 + B-C 已决策 → 传递闭包同根"
    assert next(iter(fams.values())) == "2607.00001", "有 arXiv 基的族取基 id 为根"

    data = _client().get("/api/families").json()
    group = next(f for f in data["families"] if f["family_id"] == "2607.00001")
    assert {"p_fa", "p_fb", "p_fc"} <= {v["id"] for v in group["versions"]}
    assert group["pending_alerts"] == 0
    vers = [v["id"] for v in group["versions"] if v["id"] in ("p_fa", "p_fb", "p_fc")]
    assert vers.index("p_fa") < vers.index("p_fb"), "版本链按版本号升序"


# ---------- L8/L9：families API 折叠语义 ----------

def test_families_api_archived_member_and_pending_count():
    _insert_paper("p_ga", "Grouped Beta Paper", arxiv="2603.00001", year=2024,
                  version="v1")
    _insert_paper("p_gb", "Grouped Beta Paper", arxiv="2603.00001", year=2025,
                  version="v2")
    with db.tx() as c:
        c.execute("UPDATE papers SET status='archived', superseded_by='p_gb' WHERE id='p_ga'")
        c.execute("INSERT INTO dedup_alerts(id,new_paper_id,existing_paper_id,score,"
                  "breakdown_json,reason,status,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
                  ("da_x2", "p_gb", "p_ga", 0.88, "{}", "待决策", db.now()))
    jobs.rebuild_families()   # 同基成员经族重建获得 family_id（真实流程）
    data = _client().get("/api/families").json()
    group = next(f for f in data["families"] if f["family_id"] == "2603.00001")
    statuses = {v["id"]: v["status"] for v in group["versions"]}
    assert statuses["p_ga"] == "archived", "归档成员保留在版本链中（前端折叠展示）"
    assert group["canonical_id"] == "p_gb", "canonical=最新 active"
    assert group["pending_alerts"] >= 1
    assert group["versions"][0]["id"] == "p_ga", "versions 升序：旧版在前"
