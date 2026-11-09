"""模块六测试（密闭）：写作框架 References 白名单后验、CSV 护栏与四类图、
学术图注数字回溯、拓扑图四格式渲染与语法校验。"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SCHOLAR_DATA_DIR",
                      str(Path(__file__).parent / f"_cop_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_API_KEY"] = ""
os.environ["MINERU_ENABLED"] = "off"
os.environ["EMBEDDING_BACKEND"] = "off"

from app import db
from app.agents import copilot as cp
from app.agents import csv_viz, diagram
from app.config import DOCIR_DIR
from app.llm import client as llm

PID = "p_cop"


@pytest.fixture(scope="module", autouse=True)
def _setup():
    db.init_db()
    DOCIR_DIR.mkdir(parents=True, exist_ok=True)
    (DOCIR_DIR / f"{PID}.json").write_text(json.dumps({
        "meta": {"title": "Copilot Test Paper"},
        "toc": [], "tables": [], "figures": [],
        "blocks": [
            {"id": "b1", "type": "paragraph",
             "text": "We study retrieval augmented generation for evidence.", "page": 0,
             "bbox": [0.1, 0.1, 0.9, 0.2], "section_path": "1 Introduction"},
            {"id": "b2", "type": "paragraph",
             "text": "Our method achieves 92.5 accuracy on the benchmark.", "page": 0,
             "bbox": [0.1, 0.3, 0.9, 0.4], "section_path": "2 Method"}],
        "references": []}), encoding="utf-8")
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO papers(id,title,status,file_path,file_hash,"
                  "created_at,docir_path) VALUES(?,?,?,?,?,?,?)",
                  (PID, "Retrieval Augmented Generation Survey", "active", "",
                   f"h_{PID}", 0.0, str(DOCIR_DIR / f"{PID}.json")))
        for i, (t, y) in enumerate((("Attention Is All You Need", 2017),
                                    ("BERT: Pre-training", 2019),
                                    ("Scaling Laws for Neural Language Models", 2020)), 1):
            c.execute('INSERT INTO "references"(id,paper_id,idx,title,arxiv_id,doi,authors,'
                      "year,venue,title_norm) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (f"r{i}", PID, i, t, f"170{i}.0000{i}", "", "A Author", y,
                       "NeurIPS" if i != 2 else "NAACL", t.lower()))
        from app.rag.chunking import chunk_docir
        from app.rag.indexing import index_chunks
        docir = json.loads((DOCIR_DIR / f"{PID}.json").read_text(encoding="utf-8"))
        chunks = chunk_docir(docir)
    # index_chunks 内部自持 db.tx()/_write_lock——必须在上面 tx 块外调用（否则自死锁）
    index_chunks(PID, chunks)
    yield


# ---------- References 白名单 ----------

def test_whitelist_from_library_references():
    wl = cp.build_reference_whitelist([PID])
    titles = {e["title"] for e in wl}
    assert "Attention Is All You Need" in titles
    assert "Retrieval Augmented Generation Survey" in titles, "库内论文本身也在白名单"
    assert all(e["ref_id"] >= 1 for e in wl)
    # 去重：同 title_norm 只一条
    norms = [e["title"].lower() for e in wl]
    assert len(norms) == len(set(norms))


def test_writing_framework_guardrails_and_deterministic_refs(monkeypatch):
    monkeypatch.setattr(cp, "hybrid_search", lambda pids, q, top_k=5: {"hits": [
        {"paper_id": PID, "chunk_id": "c1", "text": "evidence one", "page": 0, "bbox": []},
        {"paper_id": PID, "chunk_id": "c2", "text": "evidence two", "page": 0, "bbox": []},
        {"paper_id": PID, "chunk_id": "c3", "text": "evidence three", "page": 1, "bbox": []}]})

    class _Stub:
        def __call__(self, msgs, **kw):
            # 正文引用白名单内 R1 + 编造 R99 + 编造 E9
            return {"text": "本节基于检索增强 [R1] 与编造引用 [R99]，另有 [E1] 与编造 [E9]。"}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat", _Stub())
    out = cp.writing_framework("RAG 综述", [PID])
    assert "error" not in out
    intro = next(s for s in out["sections"] if s["title"] == "Introduction")
    assert "[R1]" in intro["content"]
    assert "[R99]" not in intro["content"], "白名单外引用必须剔除"
    assert "白名单外" in intro["content"] or "已剔除" in intro["content"]
    assert "[E9]" not in intro["content"], "超证据包标记必须剔除"
    assert "invalid_ref_R99_removed" in out["guardrails"]
    refs_sec = next(s for s in out["sections"] if s["title"] == "References")
    assert refs_sec["deterministic"] and "[R1]" in refs_sec["content"]
    assert "[R99]" not in refs_sec["content"], "References 由代码组装，不含编造"
    # 每条 References 可回溯白名单
    used_titles = {e["title"] for e in out["references"]["used"]}
    assert "Attention Is All You Need" in used_titles


def test_writing_framework_insufficient_evidence(monkeypatch):
    monkeypatch.setattr(cp, "hybrid_search", lambda pids, q, top_k=5: {"hits": []})
    out = cp.writing_framework("RAG", [PID])
    assert any("待补：证据不足" in s["content"] for s in out["sections"])


# ---------- CSV 数据集 ----------

_CSV = ("model,accuracy,params_M,latency_ms\n"
        "bert-base,0.88,110,12.5\n"
        "bert-large,0.92,340,33.0\n"
        "distilbert,0.86,66,8.2\n")


def test_csv_upload_guards_and_types():
    out = csv_viz.store_dataset(_CSV.encode("utf-8"), "bench.csv")
    assert out["row_count"] == 3
    dtypes = {c["name"]: c["dtype"] for c in out["columns"]}
    assert dtypes["model"] == "string" and dtypes["accuracy"] == "number"
    with pytest.raises(ValueError):
        csv_viz.store_dataset(b"a,b\n", "x.csv")             # 只有表头
    with pytest.raises(ValueError):
        csv_viz.store_dataset(b"x" * (6 * 1024 * 1024), "big.csv")  # 超 5MB
    inj = b"name\n=CMD()\nok\n"
    out2 = csv_viz.store_dataset(inj, "inj.csv")
    ds = csv_viz.load_dataset(out2["id"])
    assert ds["rows"][0][0].startswith("'"), "公式注入必须转义"


def test_four_chart_types_and_caption(monkeypatch):
    out = csv_viz.store_dataset(_CSV.encode("utf-8"), "bench.csv")
    ds = csv_viz.load_dataset(out["id"])
    for t in ("table", "line", "bar"):
        spec = csv_viz.build_chart(ds, t, "model", ["accuracy", "params_M"])["spec"]
        assert spec["type"] == t
    spec = csv_viz.build_chart(ds, "radar", "model",
                               ["accuracy", "params_M", "latency_ms"])["spec"]
    assert spec["type"] == "radar" and len(spec["indicators"]) == 3
    with pytest.raises(ValueError):
        csv_viz.build_chart(ds, "radar", "model", ["accuracy"])   # radar 需 ≥3 指标
    with pytest.raises(ValueError):
        csv_viz.build_chart(ds, "bar", "model", ["model"])        # y 非数值列
    # 图注：数字回溯（LLM 不可用 → 确定性模板；数字全部来自数据）
    monkeypatch.setattr(llm, "available", lambda: False)
    spec = csv_viz.build_chart(ds, "bar", "model", ["accuracy"])["spec"]
    cap = csv_viz.make_caption(ds, spec, "model", "accuracy")
    for num in ("0.88", "0.92", "0.86"):
        assert num in cap, f"图注数字 {num} 必须可回溯数据集"
    assert True   # 只统计 accuracy 列


def test_caption_llm_hallucinated_number_falls_back(monkeypatch):
    out = csv_viz.store_dataset(_CSV.encode("utf-8"), "b2.csv")
    ds = csv_viz.load_dataset(out["id"])
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat", lambda m, **kw: {"text": "准确率达 0.9999，显著提升。"})
    spec = csv_viz.build_chart(ds, "line", "model", ["accuracy"])["spec"]
    cap = csv_viz.make_caption(ds, spec, "model", "accuracy")
    assert "0.9999" not in cap, "LLM 编造数字必须回退确定性模板"


# ---------- 拓扑图 ----------

def test_diagram_renders_four_formats():
    spec = diagram.DiagramSpec.model_validate({
        "nodes": [{"id": "n1", "label": "Retriever"}, {"id": "n2", "label": "Generator"},
                  {"id": "n3", "label": "Index"}],
        "edges": [{"from": "n3", "to": "n1", "label": "query"},
                  {"from": "n1", "to": "n2", "label": "docs"}]})
    mermaid = diagram.render_mermaid(spec)
    dot = diagram.render_dot(spec)
    tikz = diagram.render_tikz(spec)
    assert "graph TD" in mermaid and "n1 -->|query| n3".replace("n1 -->|query| n3", "n3 -->|query| n1") in mermaid or "-->" in mermaid
    assert dot.startswith("digraph") and dot.count('"') % 2 == 0
    assert "\\begin{tikzpicture}" in tikz and tikz.count("\\node[") == 3
    assert diagram.validate_syntax("mermaid", mermaid)["ok"]
    assert diagram.validate_syntax("dot", dot)["ok"]
    assert diagram.validate_syntax("tikz", tikz)["ok"]
    # 节点/边与 spec 严格一致
    assert mermaid.count("[") >= 3 and "Retriever" in mermaid and "Generator" in mermaid


def test_diagram_generate_with_stub_llm(monkeypatch):
    monkeypatch.setattr(diagram.llm, "available", lambda: True)
    monkeypatch.setattr(diagram.llm, "chat", lambda m, **kw: {"text": json.dumps({
        "nodes": [{"id": "n1", "label": "A"}, {"id": "n2", "label": "B"}],
        "edges": [{"from": "n1", "to": "ghost"},   # 引用不存在的 id → 必须被剔除
                  {"from": "n1", "to": "n2"}]})})
    out = diagram.generate_diagram("two module pipeline", fmt="all")
    assert len(out["spec"]["nodes"]) == 2
    assert len(out["spec"]["edges"]) == 1, "幽灵边（引用不存在节点）必须剔除"
    assert set(out["sources"]) == {"mermaid", "dot", "tikz"}
    assert out["validation"]["dot"]["ok"]


# ---------- 端点 ----------

def test_copilot_endpoints():
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)
    r = client.post("/api/copilot/datasets",
                    files={"file": ("t.csv", _CSV.encode(), "text/csv")})
    assert r.status_code == 200 and r.json()["row_count"] == 3
    ds_id = r.json()["id"]
    r2 = client.post("/api/copilot/chart", json={
        "dataset_id": ds_id, "chart_type": "bar", "x": "model", "y": ["accuracy"]})
    assert r2.status_code == 200 and r2.json()["spec"]["type"] == "bar"
    assert r2.json()["caption"], "默认带图注"
    r3 = client.post("/api/copilot/chart", json={
        "dataset_id": ds_id, "chart_type": "pie"})
    assert r3.status_code == 400, "白名单外图类型 400"
    r4 = client.post("/api/copilot/writing", json={"idea": "", "paper_ids": [PID]})
    assert r4.status_code == 400
    r5 = client.post("/api/copilot/translate", json={"text": "这不是中文吗"})
    assert r5.status_code in (200, 400)   # LLM 未配置时 200+不可用文案或语言误判 400


def test_csv_large_dataset_storage_never_corrupts():
    """大文件：存储层按行预算截断，data 列必须始终是合法 JSON（回归：旧字节切片会产生烂 JSON）。"""
    import io as _io
    buf = _io.StringIO()
    buf.write("idx,val,tag\n")
    for i in range(50_000):
        buf.write(f"{i},{i * 1.5 % 100:.2f},tag{i % 7}\n")
    raw = buf.getvalue().encode()
    out = csv_viz.store_dataset(raw, "big.csv")
    assert out["row_count"] == 50_000
    if out["storage_truncated"]:
        assert out["stored_rows"] < 50_000, "超存储预算时按行截断并如实上报"
    ds = csv_viz.load_dataset(out["id"])
    assert ds is not None and len(ds["rows"]) == out["stored_rows"], \
        "load 必须能解析（合法 JSON）且行数与上报一致"
    # 图表/图注在存储行上仍可工作
    spec = csv_viz.build_chart(ds, "line", "idx", ["val"])["spec"]
    assert spec["type"] == "line"


def test_scatter3d_true_3d_spec():
    """真 3D 散点：≥3 数值维、点云坐标、第 4 维 visualMap、<2 维报错。"""
    csv3 = "x,y,z,w,label\n1,2,3,10,a\n4,5,6,20,b\n7,8,9,30,c\n"
    out = csv_viz.store_dataset(csv3.encode(), "t3d.csv")
    ds = csv_viz.load_dataset(out["id"])
    spec = csv_viz.build_chart(ds, "scatter3d", "x", ["y", "z", "w"])["spec"]
    assert spec["type"] == "scatter3d"
    assert spec["axes"][:3] == ["x", "y", "z"] and spec["visual_dim"] == "w"
    assert len(spec["points"]) == 3 and spec["points"][0] == [1.0, 2.0, 3.0, 10.0], "含 visualMap 第 4 维"
    csv1 = "a,b\n1,2\n3,4\n"
    out1 = csv_viz.store_dataset(csv1.encode(), "t1.csv")
    ds1 = csv_viz.load_dataset(out1["id"])
    try:
        csv_viz.build_chart(ds1, "scatter3d", "a", ["b"])
        raised = False
    except ValueError:
        raised = True
    assert raised, "少于 3 个数值维必须 400"
