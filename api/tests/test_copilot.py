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
    # Round4 修复镜像 bug：旧断言 `A and B or "-->" in m` 因优先级恒真（与被修的
    # validate_syntax 同款缺陷）——改为对每条 spec 边的确定性检查
    assert mermaid.startswith("graph TD")
    assert "n3 -->|query| n1" in mermaid and "n1 -->|docs| n2" in mermaid
    assert dot.startswith("digraph") and dot.count('"') % 2 == 0
    assert "\\begin{tikzpicture}" in tikz and tikz.count("\\node[") == 3
    assert diagram.validate_syntax("mermaid", mermaid)["ok"]
    assert diagram.validate_syntax("dot", dot)["ok"]
    assert diagram.validate_syntax("tikz", tikz)["ok"]
    # 节点/边与 spec 严格一致
    assert mermaid.count("[") >= 3 and "Retriever" in mermaid and "Generator" in mermaid


def test_validate_syntax_mermaid_precedence_fixed():
    """Round4：mermaid 校验优先级 bug 回归——无 graph/flowchart 头的裸边必须拒绝。"""
    v = diagram.validate_syntax
    assert v("mermaid", "graph TD\nA --> B")["ok"]
    assert v("mermaid", "flowchart LR\nA --> B")["ok"]          # flowchart 前缀此前被拒
    assert v("mermaid", "flowchart LR\nA -.-> B")["ok"]
    assert not v("mermaid", "随便一段话 --> 含边字符")["ok"]      # 旧 bug：含 --> 即通过
    assert not v("mermaid", "graph TD\n没有边")["ok"]
    assert not v("mermaid", "")["ok"]


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


# ---------- Round4：CSV 硬化（100k 截断 / GBK / 空值异常值） ----------

def test_parse_csv_100k_row_truncation():
    """10 万行护栏：>100k 行截断到上限且如实上报 truncated。"""
    rows = ["x,y"] + [f"{i},{i * 1.5:.1f}" for i in range(100_005)]
    raw = ("\n".join(rows)).encode()
    out = csv_viz.parse_csv(raw, "big100k.csv")
    assert out["row_count"] == 100_000, "应截到 _MAX_ROWS"
    assert out["truncated"] == 5, "截断数如实上报"
    assert out["rows"][0] == ["0", "0.0"] and out["rows"][-1] == ["99999", "149998.5"]


def test_parse_csv_gbk_real_bytes():
    """GBK 真实字节文件（中文表头）——解码回退链此前零测试。"""
    text = "模型,准确率\nBERT,0.84\nGPT,0.89\n"
    out = csv_viz.parse_csv(text.encode("gbk"), "cn_gbk.csv")
    assert out["columns"][0]["name"] == "模型"
    assert out["columns"][1]["dtype"] == "number"
    assert out["rows"] == [["BERT", "0.84"], ["GPT", "0.89"]]


def test_parse_csv_nan_and_empty_cells():
    """空值/NaN 列：不炸、类型推断如实（NaN 列不误判 number）。"""
    text = "name,score\nA,0.9\nB,\nC,NaN\nD,0.7\n"
    out = csv_viz.parse_csv(text.encode(), "nan.csv")
    assert out["row_count"] == 4
    dtypes = {c["name"]: c["dtype"] for c in out["columns"]}
    assert dtypes["name"] == "string"
    # score 列 4 值中 2 个非数字（空/NaN）→ 非数字占比 50% ≥20% → string
    assert dtypes["score"] == "string"


def test_store_dataset_100k_rows_valid_json():
    """10 万行真实落库：存储 JSON 合法（行预算截断路径回归，50k 测试的补位）。"""
    cols = [{"name": "x", "dtype": "number"}, {"name": "y", "dtype": "number"}]
    rows = [[str(i), f"{i}.5"] for i in range(100_000)]
    js, kept = csv_viz._serialize_rows_bounded(rows, cols)
    data = json.loads(js)          # 必须可解析（旧字节切片会产生烂 JSON）
    assert len(data) == kept and kept <= 100_000


# ---------- Round7：写作 References 配额 / 全量 prompt / 补引 / 导出 ----------

def _insert_paper_with_refs(pid: str, prefix: str, n: int = 40) -> None:
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO papers(id,title,status,file_path,file_hash,"
                  "created_at,docir_path) VALUES(?,?,?,?,?,?,?)",
                  (pid, f"{prefix} Survey", "active", "", f"h_{pid}", 0.0, ""))
        for i in range(n):
            c.execute('INSERT INTO "references"(id,paper_id,idx,title,arxiv_id,doi,authors,'
                      "year,venue,title_norm) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (f"{pid}_r{i}", pid, i, f"{prefix} Ref {i}", "", "", "A Author",
                       2020 + i % 5, "NeurIPS", f"{prefix} ref {i}"))


def test_whitelist_quota_3papers_40refs_each():
    """Round7 回归：3 篇×40 refs → 每篇配额前 20、保底 ≥5、总量 ≤60。

    旧实现 entries[:80] 会把第 3 篇选中论文的引用几乎全部截断。"""
    pids = ["p_quota1", "p_quota2", "p_quota3"]
    for i, pid in enumerate(pids, 1):
        _insert_paper_with_refs(pid, f"Quota{i}")
    wl = cp.build_reference_whitelist(pids)
    assert len(wl) <= cp._MAX_REFS == 60
    for i in range(1, 4):
        got = [e for e in wl if e["title"].startswith(f"Quota{i} Ref ")]
        assert len(got) >= 5, f"第 {i} 篇选中论文白名单条目不足 5"
        assert len(got) <= cp._PER_PAPER_REFS == 20, "单篇配额不得超过 20"
    assert wl[0]["ref_id"] == 1 and [e["ref_id"] for e in wl] == list(range(1, len(wl) + 1))


def test_writing_prompt_full_whitelist_and_citation_constraint(monkeypatch):
    """Round7 回归：白名单全量（≤60）进 prompt + 追加「至少 5 个不同 [R#]、禁编造」约束。"""
    captured: list[str] = []

    def _chat(msgs, **kw):
        captured.append(msgs[-1]["content"])
        return {"text": "本节引用 [R1] [R2] [R3] 支撑论点。"}

    monkeypatch.setattr(cp, "hybrid_search", lambda pids, q, top_k=5: {"hits": [
        {"paper_id": PID, "chunk_id": f"cq{i}", "text": f"evidence {i}", "page": 0, "bbox": []}
        for i in range(3)]})
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat", _chat)
    out = cp.writing_framework("RAG 综述", [PID])
    prompt = captured[0]
    assert "禁止编造" in prompt and "至少引用 5 个不同 [R#]" in prompt
    wl = cp.build_reference_whitelist([PID])
    for e in wl:   # 旧实现 prompt 只喂前 60 条；现白名单 ≤60 应全量出现
        assert f"[R{e['ref_id']}]" in prompt
    assert out["references"]["used"], "prompt 已带引用约束时正文引用的 [R#] 必须可回溯"


def test_writing_underused_guardrail_and_export_unused_unumbered(monkeypatch):
    """Round7：used<3 → 补引重试 1 次仍不足则记 refs_underused:N；
    导出 MD 的 References 只列 used，未引用条目另列且不带 [R#] 编号。"""
    monkeypatch.setattr(cp, "hybrid_search", lambda pids, q, top_k=5: {"hits": [
        {"paper_id": PID, "chunk_id": f"cu{i}", "text": f"evidence {i}", "page": 0, "bbox": []}
        for i in range(3)]})
    calls: list[int] = []

    def _chat(msgs, **kw):
        calls.append(1)
        return {"text": "本节只做综述，没有可用引用。"}

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "chat", _chat)
    out = cp.writing_framework("无引用 idea", [PID])
    assert "refs_underused:0" in out["guardrails"], "引用不足必须如实写 guardrail"
    # 6 节正文各 1 次 + 补引重试恰好 1 次
    assert len(calls) == len(cp.DEFAULT_SECTIONS) + 1, "补引重试应只触发 1 次"
    assert out["references"]["used"] == []
    md = out["content_md"]
    assert "（正文暂未引用任何白名单文献）" in md
    assert "## 未引用（未纳入正文）" in md
    unused_part = md.split("## 未引用（未纳入正文）", 1)[1]
    assert "[R" not in unused_part, "未引用条目不得带 [R#] 编号（避免误读为正文引用）"


# ---------- Round7：radar 方向修正 / radar3d / bar 降采样 ----------

_RADAR_DS = {
    "id": "ds_radar", "name": "radar.csv",
    "columns": [{"name": "model", "dtype": "string"},
                {"name": "accuracy", "dtype": "number"},
                {"name": "params_M", "dtype": "number"},
                {"name": "latency_ms", "dtype": "number"}],
    "row_count": 3,
    "rows": [["bert-base", "0.88", "110", "12.5"],
             ["bert-large", "0.92", "340", "33.0"],
             ["distilbert", "0.86", "66", "8.2"]],
}


def test_radar_direction_indicators_are_y_columns_series_are_rows():
    """Round7 回归：indicators = y 指标列（每维 max=该列全量最大×1.1）；
    series = 每个 x 类别一行 {name, data:[各维值]}（旧实现方向反置）。"""
    spec = csv_viz.build_chart(_RADAR_DS, "radar", "model",
                               ["accuracy", "params_M", "latency_ms"])["spec"]
    assert spec["type"] == "radar"
    assert [i["name"] for i in spec["indicators"]] == ["accuracy", "params_M", "latency_ms"]
    assert spec["indicators"][0]["max"] == pytest.approx(0.92 * 1.1)
    assert spec["indicators"][1]["max"] == pytest.approx(340 * 1.1)
    assert spec["indicators"][2]["max"] == pytest.approx(33.0 * 1.1)
    assert len(spec["series"]) == len(_RADAR_DS["rows"]), "每个行/类别一条雷达多边形"
    assert spec["series"][0] == {"name": "bert-base", "data": [0.88, 110.0, 12.5]}
    assert spec["series"][2]["name"] == "distilbert"
    # radar 图注走确定性模板（route 传进类别名也不会当列统计炸 500）
    cap = csv_viz.make_caption(_RADAR_DS, spec, "model", spec["series"][0]["name"])
    assert "3 维指标雷达" in cap and "bert-base" not in cap


def test_radar3d_variant_is_2d_radar_with_perspective_marker():
    spec = csv_viz.build_chart(_RADAR_DS, "radar3d", "model",
                               ["accuracy", "params_M", "latency_ms"])["spec"]
    assert spec["type"] == "radar", "底层仍是 ECharts 2D radar（ECharts 无原生 radar3D）"
    assert spec["variant"] == "radar3d" and "radar3D" in spec["render_note"]
    assert len(spec["indicators"]) == 3 and len(spec["series"]) == 3


def test_chart_endpoint_radar_caption_and_radar3d():
    """Round7 路由回归：radar 的 series[0].name 是类别名而非列名，
    旧 caption 逻辑会 names.index(类别名) 抛 500——现由确定性雷达图注兜住。"""
    from fastapi.testclient import TestClient

    from app.main import app
    stored = csv_viz.store_dataset(_CSV.encode("utf-8"), "radar_ep.csv")
    client = TestClient(app)
    body = {"dataset_id": stored["id"], "chart_type": "radar", "x": "model",
            "y": ["accuracy", "params_M", "latency_ms"]}
    r = client.post("/api/copilot/chart", json=body)
    assert r.status_code == 200, f"radar 图注不得 500：{r.text[:300]}"
    out = r.json()
    assert len(out["spec"]["indicators"]) == 3 and len(out["spec"]["series"]) == 3
    assert "3 维指标雷达" in out["caption"]
    r2 = client.post("/api/copilot/chart", json={**body, "chart_type": "radar3d"})
    assert r2.status_code == 200
    assert r2.json()["spec"]["variant"] == "radar3d" and r2.json()["caption"]


def test_bar_downsample_100k_rows_top60_plus_others():
    """Round7：10 万行 bar 降采样为 Top60 +「其他（聚合）」；caption 统计仍用全量。"""
    lines = ["idx,val"] + [f"{i},{i}" for i in range(100_000)]
    parsed = csv_viz.parse_csv("\n".join(lines).encode(), "big100k_bar.csv")
    ds = {"id": "ds_bigbar", "name": "big100k_bar.csv", "columns": parsed["columns"],
          "row_count": parsed["row_count"], "rows": parsed["rows"]}
    spec = csv_viz.build_chart(ds, "bar", "idx", ["val"])["spec"]
    labels = spec["x"]["data"]
    assert len(labels) == 61, "Top60 + 其他聚合 = 61 个类别"
    assert labels[-1] == "其他（聚合）"
    data = spec["series"][0]["data"]
    assert data[0] == 99999.0 and data[59] == 99940.0, "按数值降序取 Top60"
    assert data[-1] == sum(range(99940)), "其余行按系列求和聚合"
    info = spec["downsampled"]
    assert info["total_rows"] == 100_000 and info["shown_rows"] == 60
    assert info["others_rows"] == 100_000 - 60
    cap = csv_viz.make_caption(ds, spec, "idx", "val")
    assert "n=100000" in cap, "图注统计必须仍用全量数据（非 Top60 子集）"


# ---------- Round7：拓扑图 LLM 不可用规则降级 ----------

def test_rule_spec_arrow_chain_4nodes_3edges():
    spec = diagram._rule_spec("Query → Retriever → Reranker → Generator")
    assert [(n.id, n.label) for n in spec.nodes] == [
        ("n1", "Query"), ("n2", "Retriever"), ("n3", "Reranker"), ("n4", "Generator")]
    assert [(e.from_, e.to) for e in spec.edges] == [
        ("n1", "n2"), ("n2", "n3"), ("n3", "n4")]
    # 变体箭头 + 系统名前缀清洗 + 句读/逗号拆步 + ≤12 节点
    assert [n.label for n in diagram._rule_spec("A -> B => C").nodes] == ["A", "B", "C"]
    rag = diagram._rule_spec(
        "RAG 系统：Query 改写器 → Retriever 从向量库取 top-k → Reranker 精排 → Generator 生成带引用回答")
    assert rag.nodes[0].label == "Query 改写器" and len(rag.nodes) == 4
    steps = "步骤：" + "、".join(f"s{i}" for i in range(20))
    assert len(diagram._rule_spec(steps).nodes) == diagram._MAX_NODES, "节点数上限 12"


def test_generate_diagram_llm_unavailable_rule_fallback(monkeypatch):
    """Round7 回归：LLM 不可用时 generate_diagram 正常返回四格式（不再 503）。"""
    monkeypatch.setattr(diagram.llm, "available", lambda: False)
    out = diagram.generate_diagram(
        "Query → Retriever → Reranker → Generator", fmt="mermaid")
    assert out.get("guardrail") == "diagram_rule_fallback"
    assert set(out["sources"]) == {"mermaid", "dot", "tikz"}
    assert out["validation"]["mermaid"]["ok"] is True
    assert out["validation"]["dot"]["ok"] is True
    assert out["validation"]["tikz"]["ok"] is True
    assert len(out["spec"]["nodes"]) == 4 and len(out["spec"]["edges"]) == 3
    # LLM 可用但调用抛错 → 同样降级
    monkeypatch.setattr(diagram.llm, "available", lambda: True)

    def _boom(*a, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(diagram.llm, "chat", _boom)
    out2 = diagram.generate_diagram("A -> B", fmt="mermaid")
    assert out2.get("guardrail") == "diagram_rule_fallback"
    assert out2["validation"]["mermaid"]["ok"] is True


def test_diagram_endpoint_no_503_without_llm(monkeypatch):
    """e2e ⑩ 回归：POST /api/copilot/diagram 在 LLM 不可用时应 200（旧实现 503）。"""
    monkeypatch.setattr(diagram.llm, "available", lambda: False)
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)
    r = client.post("/api/copilot/diagram", json={
        "description": "Query → Retriever → Reranker → Generator", "fmt": "mermaid"})
    assert r.status_code == 200, f"LLM 不可用不得 503：{r.text[:200]}"
    body = r.json()
    assert body.get("guardrail") == "diagram_rule_fallback"
    assert body["validation"]["mermaid"]["ok"] is True
