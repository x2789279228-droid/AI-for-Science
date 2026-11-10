"""4.4 工具集成测试（密闭）：白名单矩阵 / arxiv_search(mock XML) / github_repro(mock JSON) /
sandbox 真实出图 / translate_block / call_tool 重试语义 / pdf_parse 轮询。
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SCHOLAR_DATA_DIR", str(Path(__file__).parent / f"_tl4_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_PROVIDER"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["MINIMAX_API_KEY"] = ""
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["MINERU_ENABLED"] = "off"
os.environ["EMBEDDING_BACKEND"] = "off"

from app.agents import tools
from app.agents.state import AGENT_LABEL
from app.config import settings

DATA = Path(os.environ["SCHOLAR_DATA_DIR"])

_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v1</id>
    <title>  Mamba: Linear-Time Sequence Modeling </title>
    <author><name>Albert Gu</name></author>
    <author><name>Tri Dao</name></author>
    <summary>  We propose selective state space models.  </summary>
    <published>2024-01-15T18:00:00Z</published>
  </entry>
</feed>"""

_GITHUB_JSON = json.dumps({"items": [
    {"full_name": "state-spaces/mamba", "stargazers_count": 8000,
     "pushed_at": "2024-03-01T00:00:00Z", "license": {"spdx_id": "Apache-2.0"},
     "html_url": "https://github.com/state-spaces/mamba"},
]})


@pytest.fixture()
def no_network(monkeypatch):
    """断网模拟：arxiv/github 域名请求直接抛错（验证 tool_cache 回放）。"""
    def boom(url, **kw):
        raise OSError("network down")
    monkeypatch.setattr(tools.httpx, "get", boom)
    monkeypatch.setattr(tools.httpx, "stream", boom)


# ---------- 白名单矩阵 ----------

def test_tool_registry_13_tools_and_whitelist():
    assert set(tools.TOOLS) == {
        "kb_search", "get_meta", "align_dimension", "citation_graph", "graph_metrics",
        "extract_table", "chart_spec_builder",
        "arxiv_search", "paper_download", "pdf_parse", "github_repro",
        "python_sandbox", "translate_block",
    }
    # 评分表 8 工具全部在编
    for t in ("arxiv_search", "paper_download", "pdf_parse", "kb_search",
              "github_repro", "python_sandbox", "citation_graph", "translate_block"):
        assert t in tools.TOOLS
    # explore 身份可调闭环三件套；其他 agent 不能调下载
    tools.assert_allowed("explore", "paper_download")
    tools.assert_allowed("explore", "pdf_parse")
    tools.assert_allowed("reader", "arxiv_search")
    with pytest.raises(tools.ToolDenied):
        tools.assert_allowed("reader", "paper_download")
    with pytest.raises(tools.ToolDenied):
        tools.assert_allowed("compare", "python_sandbox")
    assert AGENT_LABEL  # Agent 标签表仍完整


# ---------- arxiv_search ----------

def test_arxiv_search_live(monkeypatch):
    class R:
        text = _ARXIV_ATOM
        def raise_for_status(self): pass
    monkeypatch.setattr(tools.httpx, "get", lambda url, **kw: R())
    out = tools._arxiv_search("mamba linear-time", max_results=5)
    assert out["ok"] and out["source"] == "live"
    e = out["entries"][0]
    assert e["arxiv_id"] == "2401.12345v1"
    assert e["title"] == "Mamba: Linear-Time Sequence Modeling"
    assert e["authors"] == ["Albert Gu", "Tri Dao"]
    assert e["published"] == "2024-01-15"
    assert "/pdf/" in e["pdf_url"]
    # 成功结果已落缓存
    assert tools._tool_cache_read("arxiv", "mamba linear-time") is not None


def test_arxiv_search_offline_replays_cache(no_network):
    out = tools._arxiv_search("mamba linear-time")
    assert out["ok"] and out["source"] == "cache_offline"
    assert out["entries"][0]["arxiv_id"] == "2401.12345v1"
    assert "live_error" in out


def test_arxiv_search_no_cache_no_network(no_network, monkeypatch):
    monkeypatch.setattr(tools, "_tool_cache_read", lambda *a: None)
    out = tools._arxiv_search("never-cached-xyz")
    assert not out["ok"] and out["source"] == "none"


# ---------- github_repro ----------

def test_github_repro_scoring(monkeypatch):
    class R:
        text = _GITHUB_JSON.replace("2024-03-01", "2026-11-01")   # 活跃期内
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return json.loads(self.text)
    monkeypatch.setattr(tools.httpx, "get", lambda url, **kw: R())
    monkeypatch.setattr(settings, "github_token", "")
    out = tools._github_repro("Mamba Linear-Time Sequence Modeling")
    assert out["ok"] and out["source"] == "live"
    r0 = out["repos"][0]
    assert r0["repo"] == "state-spaces/mamba"
    # stars 8000(1.0) + 90 天内活跃(1.0) + Apache(1.0) → 满分
    assert r0["repro_score"] == 1.0


def test_repro_score_tiers():
    assert tools._repro_score({"stars": 2000, "pushed_at": "2026-11-01T00:00:00Z",
                               "license": "MIT"}) == 1.0
    assert tools._repro_score({"stars": 50, "pushed_at": "2020-01-01T00:00:00Z",
                               "license": ""}) == 0.12   # 0.4*0.3 + 0 + 0（50 星档位 0.3）
    assert tools._repro_score({"stars": 0, "pushed_at": "", "license": "MIT"}) == 0.2


def test_github_repro_offline_cache(no_network):
    out = tools._github_repro("Mamba Linear-Time Sequence Modeling")
    assert out["ok"] and out["source"] == "cache_offline"


# ---------- python_sandbox（真实子进程） ----------

def test_sandbox_stdout_and_plot():
    code = ("print('hello sandbox')\n"
            "plt.plot([1, 2], [3, 4])\n"
            "plt.savefig(FIGURE_OUT)\n")
    out = tools._python_sandbox(code)
    assert out["ok"] and "hello sandbox" in out["stdout"]
    assert out["image_path"] and out["image_path"].endswith(".png")
    png = Path(os.environ["SCHOLAR_DATA_DIR"]) / "figures" / "sandbox" / \
        Path(out["image_path"]).name
    assert png.exists() and png.read_bytes()[:4] == b"\x89PNG"


def test_sandbox_error_and_timeout_guard():
    out = tools._python_sandbox("raise ValueError('boom')")
    assert not out["ok"] and "ValueError" in out["stderr"]
    # 30s 超时防线（脚本死循环；断言快速返回而非真等 30s 太久 → 用异常路径覆盖）
    out2 = tools._python_sandbox("import sys; print(sys.flags.isolated)")
    assert out2["ok"] and out2["stdout"].strip() == "1"   # -I 隔离模式生效


# ---------- translate_block / call_tool 重试 ----------

def test_call_tool_retryable(monkeypatch):
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return {"good": True}

    monkeypatch.setitem(tools.TOOLS, "arxiv_search",
                        tools.TOOLS["arxiv_search"].__class__(
                            name="arxiv_search", desc="", params={}, fn=flaky,
                            allowed_agents={"explore"}, retryable=True))
    r = tools.call_tool("explore", "arxiv_search", None, query="x")
    assert r["ok"] and calls["n"] == 2          # 幂等工具自动重试 1 次后成功


def test_call_tool_side_effect_no_retry(monkeypatch):
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        raise OSError("always")

    monkeypatch.setitem(tools.TOOLS, "paper_download",
                        tools.TOOLS["paper_download"].__class__(
                            name="paper_download", desc="", params={}, fn=flaky,
                            allowed_agents={"explore"}))
    r = tools.call_tool("explore", "paper_download", None, arxiv_id="x")
    assert not r["ok"] and calls["n"] == 1      # 有副作用工具不重试
    assert r["failure"]["retried"] is False


def test_call_tool_denied_is_failure():
    r = tools.call_tool("reader", "paper_download", None, arxiv_id="x")
    assert not r["ok"] and "not allowed" in r["error"]


# ---------- pdf_parse 轮询 ----------

def test_pdf_parse_already_parsed(monkeypatch):
    monkeypatch.setattr(tools.db, "query",
                        lambda sql, *a: [{"docir_path": "x.json"}])
    out = tools._pdf_parse("p1", wait_s=0)
    assert out["ok"] and out["status"] == "already_parsed"


# ---------- Round5：compare 弱命中诚实降级 ----------

def test_aspect_terms_and_weak_match():
    from app.agents.tools import _aspect_terms, _weak_match
    # 拉丁/数字特征词提取；中文 token 不参与（跨语言无法字面命中）
    assert _aspect_terms("WMT 2014 英德翻译任务上的 BLEU 分数") == ["WMT", "2014", "BLEU"]
    assert _aspect_terms("预训练目标与上下文表示") == []
    assert _aspect_terms("d_model 与层数对比") == ["d_model"]
    # 弱命中：有特征词但证据全文一处不含 → True
    assert _weak_match("word based only on its context. Unlike l", ["WMT", "2014", "BLEU"])
    assert not _weak_match("28.4 BLEU on WMT 2014 En-De", ["WMT", "2014", "BLEU"])
    assert not _weak_match("任意文本", []), "无特征词（中文维度）不判弱"


def test_align_dimension_weak_match_downgrade():
    from app.agents.tools import _align_dimension
    ev = [
        # Attention：含 BLEU 的真实证据（全文含特征词，quote 截断也不影响——判全文）
        {"aspect": "WMT 2014 英德翻译任务上的 BLEU 分数", "paper_id": "att",
         "text": "The big model achieves 28.4 BLEU on WMT 2014 English-to-German. " + "pad " * 60,
         "chunk_id": "c1", "page": 0, "block_ids": ["b1"], "score": 1.0},
        # BERT：dense 泛相似召回的无关块（全文不含 WMT/BLEU）→ 应诚实降级为缺失
        {"aspect": "WMT 2014 英德翻译任务上的 BLEU 分数", "paper_id": "bert",
         "text": "word based only on its context. Unlike left-to-right models, " + "pad " * 60,
         "chunk_id": "c2", "page": 3, "block_ids": ["b9"], "score": 0.8},
        # 中文维度无特征词 → 不降级（保持 found）
        {"aspect": "预训练目标", "paper_id": "att",
         "text": "encoder-decoder trained end to end", "chunk_id": "c3", "page": 1,
         "block_ids": ["b2"], "score": 0.9},
    ]
    out = _align_dimension(ev, ["att", "bert"])
    by_dim = {r["dimension"]: r for r in out["rows"]}
    bleu = by_dim["WMT 2014 英德翻译任务上的 BLEU 分数"]
    cells = {c["paper_id"]: c for c in bleu["cells"]}
    assert cells["att"]["found"] is True and "quote" in cells["att"]
    assert cells["bert"]["found"] is False
    assert cells["bert"].get("reason") == "weak_match", "弱命中必须带可解释 reason"
    pre = by_dim["预训练目标"]
    pre_cells = {c["paper_id"]: c for c in pre["cells"]}
    assert pre_cells["att"]["found"] is True, "中文维度无特征词：真实命中不降级"
    assert pre_cells["bert"]["found"] is False
    assert pre_cells["bert"].get("reason") is None, "无命中=普通缺失，不是 weak_match"
