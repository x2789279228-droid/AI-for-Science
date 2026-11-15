"""双模式（doc_mode/doc_type）：普通文档管线门控 + 学术端点守卫 + chat 白名单/混选防线。

覆盖面：
- 迁移：papers.doc_type 列存在，缺省 paper
- 上传：doc_type=general 落库；同文件两种类型各存一份（复合唯一索引）；非法值 400
- 解析门控：普通文档跳过 arXiv 版本推导（fixture 带 arXiv 行也不落 arxiv_id）、跳过语义去重
- API：列表 doc_type 过滤；restore/dedup-decision/version-diff/ref-quality 对普通文档 400
- 改名：普通文档不参与 family 归一
- chat：非法 doc_mode 400；普通模式禁 graph/writer 400；混选两类文档 400；
  隐式全库选文按 doc_mode 作用域（runner 打桩捕获）
"""
from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db

_DATA = Path(__import__("os").environ["SCHOLAR_DATA_DIR"])


def _client() -> TestClient:
    from app.main import app

    return TestClient(app)


def _make_pdf(path: Path, title: str, arxiv_line: str | None = None) -> Path:
    import hashlib

    from tests.fixtures_pdf import make_paper_pdf

    # helv 字体不渲染 CJK——ASCII 唯一标记保证每份 fixture 字节不同（否则同 hash
    # 触发文件级去重/复合唯一索引，测试间互相污染）
    tag = hashlib.md5(title.encode("utf-8")).hexdigest()[:10]
    kw: dict = {"body_facts": [f"fact-{tag}: recall 92.4 percent on the benchmark."],
                "refs": [f"Marker-{tag} et al. 2024. Reference entry for parsing."]}
    if arxiv_line:
        kw["arxiv_line"] = arxiv_line
    make_paper_pdf(path, title, **kw)
    return path


def _upload(client: TestClient, path: Path, doc_type: str = "paper") -> dict:
    r = client.post("/api/papers",
                    files={"file": (path.name, io.BytesIO(path.read_bytes()), "application/pdf")},
                    data={"doc_type": doc_type})
    assert r.status_code == 200, r.text
    return r.json()


def _upload_raw(client: TestClient, content: bytes, name: str = "t.pdf",
                doc_type: str = "paper") -> object:
    return client.post("/api/papers",
                       files={"file": (name, io.BytesIO(content), "application/pdf")},
                       data={"doc_type": doc_type})


def _wait(client: TestClient, job_id: str, timeout_s: float = 120) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            return j
        time.sleep(0.2)
    return {"status": "timeout"}


def _doc_row(paper_id: str) -> dict:
    rows = db.query("SELECT * FROM papers WHERE id=?", (paper_id,))
    assert rows, f"paper {paper_id} 不存在"
    return dict(rows[0])


def test_migration_doc_type_column():
    cols = [r["name"] for r in db.query("SELECT name FROM pragma_table_info('papers')")]
    assert "doc_type" in cols
    # 唯一索引已升级为复合（同文件两种类型可各存一份）
    idx = {r["name"] for r in db.query("SELECT name FROM pragma_index_list('papers')")}
    assert "idx_papers_file_type_hash" in idx


def test_upload_general_list_filter_and_invalid_type():
    p = _make_pdf(_DATA / "dm_gen_a.pdf", "普通模式测试手册A")
    with _client() as c:
        up = _upload(c, p, doc_type="general")
        assert up.get("job_id")
        assert _wait(c, up["job_id"])["status"] == "done"
        row = _doc_row(up["paper_id"])
        assert row["doc_type"] == "general"

        # 列表过滤
        gens = c.get("/api/papers", params={"doc_type": "general"}).json()["papers"]
        papers = c.get("/api/papers", params={"doc_type": "paper"}).json()["papers"]
        assert up["paper_id"] in {x["id"] for x in gens}
        assert up["paper_id"] not in {x["id"] for x in papers}

        # 非法 doc_type → 400
        r = _upload_raw(c, p.read_bytes(), name="x.pdf", doc_type="novel")
        assert r.status_code == 400


def test_general_parse_skips_academic_fields():
    """普通文档即使首页带 arXiv 行也不落 arxiv_id/版本（学术身份字段显式清空）。"""
    p = _make_pdf(_DATA / "dm_gen_b.pdf", "普通模式测试教材B",
                  arxiv_line="arXiv:2401.12345v2")
    with _client() as c:
        up = _upload(c, p, doc_type="general")
        assert _wait(c, up["job_id"])["status"] == "done"
        row = _doc_row(up["paper_id"])
        assert row["arxiv_id"] == ""
        assert row["version"] == ""
        assert row["family_id"] == ""
        assert row["page_count"] > 0
        # 无语义去重签名（跳过）
        assert row["content_sig"] is None
        assert row["paper_vec"] is None


def test_same_file_two_types_coexist():
    p = _make_pdf(_DATA / "dm_dual.pdf", "双类型同文件手册")
    content = p.read_bytes()
    with _client() as c:
        r1 = _upload_raw(c, content, name="dual.pdf", doc_type="paper")
        r2 = _upload_raw(c, content, name="dual.pdf", doc_type="general")
        assert r1.status_code == 200 and r2.status_code == 200
        j1, j2 = r1.json(), r2.json()
        assert j1["paper_id"] != j2["paper_id"]          # 两种类型各存一份
        assert j2.get("dedup") is None                   # 跨类型不复用
        # 同类型再传 → 文件级复用
        r3 = _upload_raw(c, content, name="dual.pdf", doc_type="general")
        assert r3.status_code == 200
        assert r3.json().get("dedup") == "exact_file"


def test_general_docs_no_semantic_dedup_alerts():
    """两份主题相近的普通文档之间不产生语义去重预警（跳过 post_parse_dedup）。"""
    pa = _make_pdf(_DATA / "dm_gen_c.pdf", "深度学习入门讲义")
    pb = _make_pdf(_DATA / "dm_gen_d.pdf", "深度学习入门讲义第二版")
    with _client() as c:
        u1 = _upload(c, pa, doc_type="general")
        u2 = _upload(c, pb, doc_type="general")
        assert _wait(c, u1["job_id"])["status"] == "done"
        assert _wait(c, u2["job_id"])["status"] == "done"
        d1 = c.get(f"/api/papers/{u1['paper_id']}").json()
        d2 = c.get(f"/api/papers/{u2['paper_id']}").json()
        assert "dedup_alerts" not in d1 and "dedup_alert" not in d1
        assert "dedup_alerts" not in d2 and "dedup_alert" not in d2


def test_rename_general_no_family():
    p = _make_pdf(_DATA / "dm_gen_e.pdf", "改名测试报告")
    with _client() as c:
        up = _upload(c, p, doc_type="general")
        assert _wait(c, up["job_id"])["status"] == "done"
        r = c.patch(f"/api/papers/{up['paper_id']}", json={"title": "改名后的报告"})
        assert r.status_code == 200
        row = _doc_row(up["paper_id"])
        assert row["title"] == "改名后的报告"
        assert row["family_id"] == ""     # 普通文档不参与 family 归一


def test_paper_only_endpoints_guard_general():
    p = _make_pdf(_DATA / "dm_gen_f.pdf", "守卫测试合同")
    with _client() as c:
        up = _upload(c, p, doc_type="general")
        pid = up["paper_id"]
        assert _wait(c, up["job_id"])["status"] == "done"
        assert c.post(f"/api/papers/{pid}/restore").status_code == 400
        r = c.post(f"/api/papers/{pid}/dedup-decision",
                   json={"decision": "coexist", "target_id": pid})
        assert r.status_code == 400
        r = c.post(f"/api/papers/{pid}/version-diff", json={"base_id": pid})
        assert r.status_code == 400
        r = c.get(f"/api/papers/{pid}/ref-quality")
        assert r.status_code == 400


def test_chat_rejects_invalid_doc_mode_and_agent():
    with _client() as c:
        r = c.post("/api/chat", json={"question": "这是什么？", "paper_ids": [],
                                      "doc_mode": "movie"})
        assert r.status_code == 400
        r = c.post("/api/chat", json={"question": "这是什么？", "paper_ids": [],
                                      "doc_mode": "general", "agent": "graph"})
        assert r.status_code == 400
        r = c.post("/api/chat", json={"question": "这是什么？", "paper_ids": [],
                                      "doc_mode": "general", "agent": "writer"})
        assert r.status_code == 400


def test_chat_rejects_mixed_selection():
    p1 = _make_pdf(_DATA / "dm_mix_a.pdf", "混选测试论文")
    p2 = _make_pdf(_DATA / "dm_mix_b.pdf", "混选测试教材")
    with _client() as c:
        u1 = _upload(c, p1, doc_type="paper")
        u2 = _upload(c, p2, doc_type="general")
        r = c.post("/api/chat", json={"question": "对比", "doc_mode": "general",
                                      "paper_ids": [u1["paper_id"], u2["paper_id"]]})
        assert r.status_code == 400
        # 纯普通选文 + 正确 doc_mode → 不在白名单校验处被拒（进入图执行）
        r2 = c.post("/api/chat", json={"question": "这份文档讲什么", "doc_mode": "general",
                                       "paper_ids": [u2["paper_id"]]})
        assert r2.status_code == 200


def test_chat_implicit_library_scoped_by_doc_mode(monkeypatch):
    """隐式全库选文按 doc_mode 作用域：普通模式只取 general 文档。

    注：选文 ≤8 截断护栏可能把最新文档截掉（此前测试遗留了大量 general 文档），
    故隐式路径做方向性断言（不含 paper、全部 ⊆ general）；doc_mode 透传与显式
    选文经第二次调用精确断言。
    """
    from app.agents import runner

    captured: dict = {}

    def fake_run_stream(question, paper_ids, q, **kwargs):
        captured["paper_ids"] = list(paper_ids)
        captured["doc_mode"] = kwargs.get("doc_mode")
        q.put(("final", {"answer": "ok", "claims": [], "mode": "none",
                         "guardrails": [], "trace": []}))
        q.put(("done", {}))

    monkeypatch.setattr(runner, "run_stream", fake_run_stream)

    pa = _make_pdf(_DATA / "dm_imp_a.pdf", "隐式选文学术论文")
    pb = _make_pdf(_DATA / "dm_imp_b.pdf", "隐式选文普通手册")
    with _client() as c:
        u1 = _upload(c, pa, doc_type="paper")
        u2 = _upload(c, pb, doc_type="general")

        def _wait_captured():
            deadline = time.time() + 5
            while "doc_mode" not in captured and time.time() < deadline:
                time.sleep(0.05)

        r = c.post("/api/chat", json={"question": "库里有什么", "paper_ids": [],
                                      "doc_mode": "general"})
        assert r.status_code == 200
        _wait_captured()
        assert captured.get("doc_mode") == "general"
        got = captured.get("paper_ids", [])
        # 作用域：隐式查询只含 general 文档（学术文献绝不混入）
        assert u1["paper_id"] not in got
        general_ids = {r["id"] for r in db.query(
            "SELECT id FROM papers WHERE COALESCE(doc_type,'paper')='general'")}
        assert got and set(got) <= general_ids

        # 显式选文：doc_mode 与 paper_ids 原样透传 runner
        captured.clear()
        r2 = c.post("/api/chat", json={"question": "这份文档讲什么",
                                       "paper_ids": [u2["paper_id"]],
                                       "doc_mode": "general"})
        assert r2.status_code == 200
        _wait_captured()
        assert captured.get("paper_ids") == [u2["paper_id"]]
        assert captured.get("doc_mode") == "general"


def test_general_disabled_tools_backstop():
    """assert_allowed 硬兜底：general 模式下学术工具被拒（ContextVar 传播）。"""
    from app.agents.tools import ToolDenied, assert_allowed, set_doc_mode

    set_doc_mode("paper")
    assert_allowed("graph", "citation_graph")          # 专业模式允许
    set_doc_mode("general")
    with pytest.raises(ToolDenied):
        assert_allowed("graph", "citation_graph")
    with pytest.raises(ToolDenied):
        assert_allowed("reader", "arxiv_search")
    assert_allowed("reader", "kb_search")              # 通用工具不受影响
    set_doc_mode("paper")


def test_general_prompt_slots_and_scholar_untouched():
    """双提示词并存：general 槽位可解析且为文档措辞；学术槽位原文未动。"""
    from app.llm import prompts
    from app.rag.answering import SYSTEM_QA, SYSTEM_QA_GENERAL, build_qa_messages

    g = prompts.get_prompt("qa", "system_qa_general")
    assert g is not None and "文档问答助手" in g.text
    s = prompts.get_prompt("qa", "system_qa")
    assert s is not None and "科研文献问答助手" in s.text
    assert s.text != g.text
    # 内置兜底常量同步存在（YAML 损坏时回退）
    assert "文档问答助手" in SYSTEM_QA_GENERAL
    assert "科研文献问答助手" in SYSTEM_QA
    # build_qa_messages 按 doc_mode 选槽
    msgs_g = build_qa_messages("q", [], doc_mode="general")
    msgs_s = build_qa_messages("q", [], doc_mode="paper")
    assert "文档问答助手" in msgs_g[0]["content"]
    assert "科研文献问答助手" in msgs_s[0]["content"]
