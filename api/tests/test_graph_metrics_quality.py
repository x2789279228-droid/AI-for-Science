"""图谱指标质量回归（W3）：介数中心性不再因缺 scipy 恒 0；含金量统计双写。

背景：`_graph_metrics` 曾把整个 networkx 分支放在 `import scipy` 之后，而 scipy
不是项目依赖（pyproject 未声明、venv 未安装）→ 恒走 ImportError 降级分支，全部
节点 betweenness=0；同时 `_build_graph` 只把 class_stat/mention_count 挂响应顶层，
前端读 graph.insights.class_stat → 含金量 chip 永不渲染。

覆盖：
1. 链式有向图 A→B→C→D：backend 含 networkx、中间节点介数 > 0、PageRank 归一、字段齐全；
2. nx.pagerank 抛异常（等价缺 scipy 场景）：介数仍非 0、backend 如实标注组合；
3. 无 networkx：保持纯 Python PageRank、介数全 0、backend=pagerank_py；
4. `_build_graph`：weighted 时 insights.class_stat/mention_count 双写且顶层保留；
5. `GET /api/graph`：真实路由+缓存层返回 insights.class_stat（前端 chip 数据源）。
"""
from __future__ import annotations

import builtins

import networkx as nx
import pytest

_NODE_FIELDS = {"paper_id", "title", "pagerank", "in_degree", "out_degree", "betweenness"}
_METRICS_FIELDS = {"backend", "nodes", "edge_count"}


def _chain_graph() -> dict:
    """A→B→C→D 引用链：B、C 是介数中心性非 0 的中间节点（修复前两者也是 0）。"""
    return {
        "nodes": [
            {"id": "a", "title": "Paper A"},
            {"id": "b", "title": "Paper B"},
            {"id": "c", "title": "Paper C"},
            {"id": "d", "title": "Paper D"},
        ],
        "edges": [
            {"source": "a", "target": "b"},
            {"source": "b", "target": "c"},
            {"source": "c", "target": "d"},
        ],
        "external_count": 0,
    }


def _by_paper(metrics: dict) -> dict[str, dict]:
    return {n["paper_id"]: n for n in metrics["nodes"]}


def test_graph_metrics_betweenness_not_zero_on_chain():
    """有向链图：networkx 介数真实计算（修复前 backend=pagerank_py、介数全 0）。"""
    from app.agents.tools import _graph_metrics

    m = _graph_metrics(_chain_graph())

    assert set(m) == _METRICS_FIELDS                       # 返回字段结构不变
    assert "networkx" in m["backend"]                      # 不再因缺 scipy 整体降级
    assert m["edge_count"] == 3
    assert all(set(n) == _NODE_FIELDS for n in m["nodes"])  # 每节点字段不变

    by_id = _by_paper(m)
    assert set(by_id) == {"a", "b", "c", "d"}
    # 介数：中间节点 > 0，端点为 0（方向敏感的有向介数）
    assert by_id["b"]["betweenness"] > 0
    assert by_id["c"]["betweenness"] > 0
    assert by_id["a"]["betweenness"] == 0
    assert by_id["d"]["betweenness"] == 0
    # 度统计方向：source→target
    assert (by_id["a"]["out_degree"], by_id["a"]["in_degree"]) == (1, 0)
    assert (by_id["b"]["out_degree"], by_id["b"]["in_degree"]) == (1, 1)
    assert (by_id["d"]["out_degree"], by_id["d"]["in_degree"]) == (0, 1)
    # PageRank 归一且按降序排列（返回排序不变）
    total = sum(n["pagerank"] for n in m["nodes"])
    assert total == pytest.approx(1.0, abs=1e-3)
    ranks = [n["pagerank"] for n in m["nodes"]]
    assert ranks == sorted(ranks, reverse=True)


def test_graph_metrics_pagerank_fallback_keeps_betweenness(monkeypatch):
    """nx.pagerank 失败（等价缺 scipy）→ 回退 _pagerank_py，但介数仍由 networkx 算出。"""
    from app.agents.tools import _graph_metrics

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated: nx.pagerank requires scipy")

    monkeypatch.setattr(nx, "pagerank", _boom)

    m = _graph_metrics(_chain_graph())

    assert m["backend"] == "networkx_betweenness_pagerank_py"   # 如实标注实现组合
    by_id = _by_paper(m)
    assert by_id["b"]["betweenness"] > 0 and by_id["c"]["betweenness"] > 0
    total = sum(n["pagerank"] for n in m["nodes"])
    assert total == pytest.approx(1.0, abs=1e-3)                # 纯 Python 回退仍归一


def test_graph_metrics_without_networkx_keeps_pure_python(monkeypatch):
    """无 networkx 环境：保持现状——纯 Python PageRank、介数空、backend=pagerank_py。"""
    from app.agents.tools import _graph_metrics

    real_import = builtins.__import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "networkx" or name.startswith("networkx."):
            raise ImportError("No module named 'networkx'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    m = _graph_metrics(_chain_graph())

    assert m["backend"] == "pagerank_py"
    assert all(n["betweenness"] == 0.0 for n in m["nodes"])
    total = sum(n["pagerank"] for n in m["nodes"])
    assert total == pytest.approx(1.0, abs=1e-3)


# ---------------- _build_graph / GET /api/graph：含金量统计契约 ----------------

_CLASS_STAT = {"substantive": 3, "background": 2, "perfunctory": 1}
_MENTION_COUNT = 6


def _fake_citation_graph(ids: list[str]) -> dict:
    return _chain_graph()


def _fake_enrich(graph: dict) -> dict:
    """用真实 _graph_metrics 产出 backend/介数，其余 enrich 字段用最小假数据。"""
    from app.agents.tools import _graph_metrics

    metrics = _graph_metrics(graph)
    nodes = [{**m, "year": 2020, "community": 0, "time_rank": None,
              "role": "cornerstone" if m["paper_id"] == "a" else ""}
             for m in metrics["nodes"]]
    return {
        "backend": metrics["backend"],
        "edge_count": metrics["edge_count"],
        "nodes": nodes,
        "edges": graph["edges"],
        "external_count": graph.get("external_count", 0),
        "communities": [{"id": 0, "size": len(nodes), "top_paper": "a"}],
        "modularity": 0.0,
    }


def _fake_classify_citations(paper_ids: list[str], graph: dict | None = None) -> dict:
    return {
        "edges": [{"source": e["source"], "target": e["target"],
                   "weight": 1.0, "examples": []} for e in _chain_graph()["edges"]],
        "class_stat": dict(_CLASS_STAT),
        "mention_count": _MENTION_COUNT,
    }


@pytest.fixture()
def graph_route_deps(monkeypatch):
    """把路由层三个重依赖打成假实现（不触库/不跑 DocIR 扫描），保留真实 _build_graph。"""
    from app.api_routes import graph_routes

    monkeypatch.setattr(graph_routes, "_citation_graph", _fake_citation_graph)
    monkeypatch.setattr(graph_routes, "enrich", _fake_enrich)
    monkeypatch.setattr(graph_routes, "classify_citations", _fake_classify_citations)
    graph_routes._graph_cache.clear()
    return graph_routes


def test_build_graph_weighted_writes_stats_into_insights(graph_route_deps):
    g = graph_route_deps._build_graph(["a", "b", "c", "d"], weighted=True)

    # 新契约：insights 内可直接读到（前端 chip 修复后的读取路径）
    assert g["insights"]["class_stat"] == _CLASS_STAT
    assert g["insights"]["mention_count"] == _MENTION_COUNT
    # 兼容旧前端/缓存：顶层字段保留
    assert g["class_stat"] == _CLASS_STAT
    assert g["mention_count"] == _MENTION_COUNT
    # 角色汇总不受影响（enrich 产物节点用 paper_id）
    assert [n["paper_id"] for n in g["insights"]["cornerstone"]] == ["a"]
    # 加权边合并正常
    assert g["edges"][0]["weight"] == 1.0


def test_build_graph_unweighted_has_no_class_stat(graph_route_deps):
    g = graph_route_deps._build_graph(["a", "b", "c", "d"], weighted=False)

    assert "class_stat" not in g and "mention_count" not in g
    assert "class_stat" not in g["insights"] and "mention_count" not in g["insights"]


def test_graph_endpoint_exposes_insights_class_stat(graph_route_deps):
    """GET /api/graph 响应：insights.class_stat 可读（前端 chip 的数据源），且缓存命中仍保留。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        r1 = client.get("/api/graph",
                        params={"paper_ids": "a,b,c,d", "weighted": "true"})
        assert r1.status_code == 200, r1.text
        body = r1.json()
        assert body["insights"]["class_stat"] == _CLASS_STAT
        assert body["insights"]["mention_count"] == _MENTION_COUNT
        assert body["class_stat"] == _CLASS_STAT           # 顶层兼容字段仍在
        assert "networkx" in body["backend"]
        assert any(n["betweenness"] > 0 for n in body["nodes"])
        assert body["cached"] is False

        # 二次请求命中 300s TTL 缓存，insights 字段随缓存载荷完整返回
        r2 = client.get("/api/graph",
                        params={"paper_ids": "a,b,c,d", "weighted": "true"})
        assert r2.status_code == 200, r2.text
        cached = r2.json()
        assert cached["cached"] is True
        assert cached["insights"]["class_stat"] == _CLASS_STAT
