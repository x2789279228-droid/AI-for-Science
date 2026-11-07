"""ToolSpec 工具注册表：Agent 能力的统一暴露面（Function Calling 证据链）。

- 每个 Agent 只能调用 allowed_agents 白名单内的工具（越权 → ToolDenied，计入 failures）
- call_tool 统一计时、发 tool_call/tool_result 协议事件（v1.1）与 trace 卡，
  与 retrieve_task 内联版本形状一致；业务级失败交由调用方决策（recover 策略②）
- 工具实现皆为纯函数（读库/读索引/纯变换），可单独单测
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import db
from ..rag import indexing
from .state import trace_card


class ToolDenied(PermissionError):
    """Agent 调用了白名单外的工具（guardrail 证据，不是 bug）。"""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    desc: str
    params: dict[str, str]
    allowed_agents: set[str]
    fn: Callable[..., Any]
    returns: str = ""


# ---- 工具实现（纯函数） ----

def _kb_search(paper_id: str, query: str, top_k: int = 10) -> dict:
    """混合检索（BM25+dense→RRF），单篇范围。"""
    return indexing.hybrid_search([paper_id], query, top_k=top_k)


def _get_meta(paper_id: str) -> dict:
    """论文元数据（标题/arXiv id/页数），对比表行标题与引用标注用。"""
    rows = db.query("SELECT id,title,arxiv_id,page_count,version FROM papers WHERE id=?",
                    (paper_id,))
    return dict(rows[0]) if rows else {"id": paper_id, "title": paper_id}


def _align_dimension(evidence: list[dict], paper_ids: list[str]) -> dict:
    """维度对齐：按 (aspect, paper) 分组，每格取 RRF 最优命中 → 对比表结构。

    rows 与 aspects 一一对应；某篇某维度无命中时该格为空（合成阶段须如实呈现"未找到"）。
    """
    best: dict[tuple[str, str], dict] = {}
    for h in evidence:
        key = (h.get("aspect", ""), h.get("paper_id", ""))
        cur = best.get(key)
        if cur is None or (h.get("rrf") or h.get("score") or 0) > (cur.get("rrf") or cur.get("score") or 0):
            best[key] = h
    aspects = list(dict.fromkeys(h.get("aspect", "") for h in evidence))
    rows = []
    for a in aspects:
        cells = []
        for pid in paper_ids:
            h = best.get((a, pid))
            cells.append({
                "paper_id": pid,
                "found": h is not None,
                **({} if h is None else {
                    "chunk_id": h.get("chunk_id", ""), "page": h.get("page"),
                    "section_path": h.get("section_path", ""),
                    "quote": (h.get("text") or "")[:200],
                    "score": h.get("rrf") or h.get("score") or 0,
                }),
            })
        rows.append({"dimension": a, "cells": cells})
    return {"papers": list(paper_ids), "rows": rows}


# ---- 图谱工具（Graph Agent：只由真实元数据建边，禁止编边） ----

_ARXIV_RE = re.compile(r"arXiv:(\d{4}\.\d{4,5})", re.IGNORECASE)


def _citation_graph(paper_ids: list[str]) -> dict:
    """从 references 表建库内引用图：仅保留能对回 library 论文的边（arXiv id / 精确标题）。

    自环剔除；外部引用记入 external_count 但不成边（不编造节点）。返回 nodes/edges 原料。
    """
    papers = {r["id"]: dict(r) for r in db.query(
        "SELECT id,title,arxiv_id FROM papers") if r["id"]}
    by_arxiv = {p["arxiv_id"]: pid for pid, p in papers.items() if p.get("arxiv_id")}
    by_title = {p["title"].strip().lower(): pid for pid, p in papers.items() if p.get("title")}

    nodes = [{"id": pid, "title": papers[pid].get("title") or pid,
              "arxiv_id": papers[pid].get("arxiv_id") or ""}
             for pid in papers]
    edges: list[dict] = []
    external_count = 0
    for pid in paper_ids:
        if pid not in papers:
            continue
        refs = db.query('SELECT title,arxiv_id FROM "references" WHERE paper_id=? ORDER BY idx',
                        (pid,))
        for ref in refs:
            target = None
            if ref["arxiv_id"] and ref["arxiv_id"] in by_arxiv:
                target = by_arxiv[ref["arxiv_id"]]
            elif ref["title"]:
                m = _ARXIV_RE.search(ref["title"])
                if m and m.group(1) in by_arxiv:
                    target = by_arxiv[m.group(1)]
                elif ref["title"].strip().lower() in by_title:
                    target = by_title[ref["title"].strip().lower()]
            if target is None:
                external_count += 1
            elif target != pid:  # 自环剔除
                edges.append({"source": pid, "target": target})
    # 去重边（无向键）
    seen: set[tuple[str, str]] = set()
    unique_edges: list[dict] = []
    for e in edges:
        key = tuple(sorted((e["source"], e["target"])))
        if key not in seen:
            seen.add(key)
            unique_edges.append(e)
    return {"nodes": nodes, "edges": unique_edges, "external_count": external_count,
            "source_papers": list(paper_ids)}


def _pagerank_py(nodes: list[dict], edges: list[dict], alpha: float = 0.85,
                 iters: int = 30) -> dict[str, float]:
    """纯 Python PageRank（幂迭代 + dangling 质量均摊）：无 scipy 依赖，小图足够。"""
    ids = [n["id"] for n in nodes]
    idx = {pid: i for i, pid in enumerate(ids)}
    n = len(ids)
    out_links: list[list[int]] = [[] for _ in range(n)]
    for e in edges:
        s, t = idx.get(e["source"]), idx.get(e["target"])
        if s is not None and t is not None:
            out_links[s].append(t)
    ranks = [1.0 / n] * n
    for _ in range(iters):
        dangling = sum(ranks[i] for i in range(n) if not out_links[i])
        base = (1 - alpha) / n + alpha * dangling / n
        new = [base] * n
        for i in range(n):
            if out_links[i]:
                share = alpha * ranks[i] / len(out_links[i])
                for j in out_links[i]:
                    new[j] += share
        ranks = new
    return dict(zip(ids, ranks))


def _graph_metrics(graph: dict) -> dict:
    """图谱指标：networkx+scipy 可用 → PageRank+介数；否则纯 Python PageRank（backend 如实标注）。"""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        return {"backend": "none", "nodes": [], "edge_count": 0}

    in_deg: dict[str, int] = {}
    out_deg: dict[str, int] = {}
    for e in edges:
        out_deg[e["source"]] = out_deg.get(e["source"], 0) + 1
        in_deg[e["target"]] = in_deg.get(e["target"], 0) + 1

    bet: dict[str, float] = {}
    try:
        import networkx as nx
        import scipy  # noqa: F401 —— nx.pagerank 硬依赖
        g = nx.DiGraph()
        g.add_nodes_from(n["id"] for n in nodes)
        g.add_edges_from((e["source"], e["target"]) for e in edges)
        if g.number_of_edges():
            pr = nx.pagerank(g, alpha=0.85)
            bet = nx.betweenness_centrality(g)
            backend = "networkx"
        else:
            pr = {n["id"]: 1.0 / len(nodes) for n in nodes}
            backend = "networkx_no_edges"
    except ImportError:
        pr = _pagerank_py(nodes, edges)
        backend = "pagerank_py"
    except Exception as e:  # noqa: BLE001 —— 意外失败降级为均匀分，不阻塞
        pr = {n["id"]: 1.0 / len(nodes) for n in nodes}
        backend = f"degraded:{type(e).__name__}"

    title_of = {n["id"]: n["title"] for n in nodes}
    ranked = sorted(pr.items(), key=lambda kv: -kv[1])
    out = [{
        "paper_id": pid, "title": title_of.get(pid, pid),
        "pagerank": round(score, 4),
        "in_degree": in_deg.get(pid, 0),
        "out_degree": out_deg.get(pid, 0),
        "betweenness": round(bet.get(pid, 0.0), 4),
    } for pid, score in ranked]
    return {"backend": backend, "nodes": out, "edge_count": len(edges)}


# ---- 表格工具（Viz Agent：声明式 chart_spec，禁止代码执行） ----

_MD_SEP_RE = re.compile(r"^:?-{2,}:?$")


def _parse_md_table(md: str) -> tuple[list[str], list[list[str]]]:
    """解析 Markdown 管道表格 → (headers, rows)；跳过分隔行；不足两行视为无效。"""
    lines = [l.strip() for l in (md or "").splitlines() if l.strip().startswith("|")]
    rows: list[list[str]] = []
    for l in lines:
        cells = [c.strip() for c in l.strip("|").split("|")]
        if cells and all(_MD_SEP_RE.match(c) for c in cells if c):
            continue  # |---|---| 分隔行
        rows.append(cells)
    if len(rows) < 2:
        return [], []
    return rows[0], rows[1:]


def _extract_table(paper_id: str) -> dict:
    """读取 Document IR 的表格清单（页码/标题/表头/行）；MinerU 产物仅含 markdown 时现场解析。"""
    rows = db.query("SELECT docir_path FROM papers WHERE id=?", (paper_id,))
    if not rows or not rows[0]["docir_path"]:
        return {"paper_id": paper_id, "tables": [], "parsed": False}
    data = json.loads(Path(rows[0]["docir_path"]).read_text(encoding="utf-8"))
    tables = []
    for t in (data.get("tables") or []):
        headers, body = list(t.get("headers") or []), list(t.get("rows") or [])
        if not headers and not body and t.get("markdown"):
            headers, body = _parse_md_table(t["markdown"])
        page = t.get("page")
        tables.append({"id": t.get("id", ""), "page": int(page) if str(page).isdigit() else page,
                       "title": t.get("title") or "",
                       "headers": headers, "rows": body})
    return {"paper_id": paper_id, "tables": tables, "parsed": True}


_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?%?$")


def _chart_spec_builder(table: dict, x_col: int = 0, y_col: int = 1,
                        chart_type: str = "bar") -> dict:
    """把 IR 表格变换为受 schema 约束的声明式 chart_spec（ECharts 兼容最小集）。

    白名单变换（行截取/列选择/数值解析），无任何代码执行；非法 spec 返回 invalid 原因。
    """
    headers = [str(h) for h in (table.get("headers") or [])]
    rows = table.get("rows") or []
    if chart_type not in {"bar", "line", "pie"}:
        return {"valid": False, "reason": f"chart_type '{chart_type}' 不在白名单"}
    if len(headers) < 2 or len(rows) < 1:
        return {"valid": False, "reason": "表格缺表头或无数据行"}
    x_idx, y_idx = int(x_col), int(y_col)
    if not (0 <= x_idx < len(headers) and 0 <= y_idx < len(headers)):
        return {"valid": False, "reason": "列索引越界"}

    labels: list[str] = []
    values: list[float] = []
    skipped = 0
    for r in rows[:12]:  # 行数上限，防超长表
        cells = [str(c) if c is not None else "" for c in (r or [])]
        if x_idx >= len(cells) or y_idx >= len(cells):
            skipped += 1
            continue
        v_raw = cells[y_idx].replace(",", "").strip()
        if not _NUM_RE.match(v_raw):
            skipped += 1
            continue
        labels.append(cells[x_idx][:24] or "—")
        values.append(float(v_raw.rstrip("%")))
    if not labels:
        return {"valid": False, "reason": "所选列无可解析数值（y 列须为数值）"}
    return {"valid": True,
            "spec": {"type": chart_type, "title": table.get("title") or headers[y_idx],
                     "x_label": headers[x_idx], "y_label": headers[y_idx],
                     "data": [{"label": l, "value": v} for l, v in zip(labels, values)]},
            "provenance": {"paper_id": table.get("paper_id"), "page": table.get("page"),
                           "table_id": table.get("id"), "skipped_rows": skipped}}


# ---- 注册表 ----

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in [
    ToolSpec(
        name="kb_search", desc="混合检索指定论文的全文证据块（BM25+dense→RRF）",
        params={"paper_id": "str", "query": "str", "top_k": "int"},
        allowed_agents={"reader", "compare", "graph", "writer"}, fn=_kb_search,
        returns='{"hits":[...], "channels":[...]}'),
    ToolSpec(
        name="get_meta", desc="读取论文元数据（标题/arXiv id/页数/版本）",
        params={"paper_id": "str"},
        allowed_agents={"reader", "compare", "graph"}, fn=_get_meta,
        returns='{"id","title","arxiv_id","page_count","version"}'),
    ToolSpec(
        name="align_dimension", desc="把证据池按维度×论文对齐成对比表（Compare 专属）",
        params={"evidence": "list[hit]", "paper_ids": "list[str]"},
        allowed_agents={"compare"}, fn=_align_dimension,
        returns='{"papers":[...], "rows":[{"dimension","cells":[...]}]}'),
    ToolSpec(
        name="citation_graph", desc="从 references 元数据建库内引用图（只由真实数据建边）",
        params={"paper_ids": "list[str]"},
        allowed_agents={"graph"}, fn=_citation_graph,
        returns='{"nodes":[...], "edges":[...], "external_count"}'),
    ToolSpec(
        name="graph_metrics", desc="引用图指标：PageRank/介数（networkx），失败降级度统计",
        params={"graph": "graph"},
        allowed_agents={"graph"}, fn=_graph_metrics,
        returns='{"backend","nodes":[{paper_id,title,pagerank,in_degree,...}]}'),
    ToolSpec(
        name="extract_table", desc="读取 Document IR 中的表格清单（页码/表头/行）",
        params={"paper_id": "str"},
        allowed_agents={"viz"}, fn=_extract_table,
        returns='{"paper_id","tables":[{id,page,title,headers,rows}]}'),
    ToolSpec(
        name="chart_spec_builder", desc="把 IR 表格变换为声明式 chart_spec（白名单变换，无代码执行）",
        params={"table": "table", "x_col": "int", "y_col": "int", "chart_type": "str"},
        allowed_agents={"viz"}, fn=_chart_spec_builder,
        returns='{"valid","spec","provenance"} | {"valid":false,"reason"}'),
]}


def assert_allowed(agent: str, tool: str) -> None:
    spec = TOOLS.get(tool)
    if spec is None:
        raise ToolDenied(f"unknown_tool:{tool}")
    if agent not in spec.allowed_agents:
        raise ToolDenied(f"agent '{agent}' not allowed for tool '{tool}'"
                         f" (allowed: {sorted(spec.allowed_agents)})")


def call_tool(agent: str, name: str, config: dict | None, *,
              step_id: str = "", **args) -> dict:
    """统一工具调用入口：白名单校验 → 计时 → tool_call/tool_result 事件 + trace 卡。

    step_id 用于前端 Trace 时间线点亮（计划内步骤传 plan 分配的 id，计划外传
    "align:*" 等自描述 id，TracePanel 以独立工具行渲染）。
    返回 {"ok": bool, "tool": name, "data"|"error", "elapsed_ms", "failure"|"trace_card"}；
    业务异常不上抛——调用方拿 failure 自行决策（计入 failures / 交 recover）。
    """
    t0 = time.time()
    _emit_call(config, agent, name, args, step_id)
    try:
        assert_allowed(agent, name)
        data = TOOLS[name].fn(**args)
        elapsed = int((time.time() - t0) * 1000)
        card = trace_card("tool", f"工具·{name}",
                          f"{agent} 调用成功 · {elapsed}ms", {"tool": name, "agent": agent})
        _emit_result(config, agent, name, ok=True, elapsed=elapsed,
                     summary=card["summary"], refs=[], step_id=step_id)
        return {"ok": True, "tool": name, "data": data, "elapsed_ms": elapsed,
                "trace_card": card}
    except Exception as e:  # noqa: BLE001 —— 失败也是证据：计入 failures 供 recover 决策
        elapsed = int((time.time() - t0) * 1000)
        detail = f"{type(e).__name__}: {e}"[:120]
        card = trace_card("tool", f"工具·{name}", f"{agent} 调用失败 · {detail}",
                          {"tool": name, "agent": agent, "error": detail})
        _emit_result(config, agent, name, ok=False, elapsed=elapsed, summary=card["summary"],
                     refs=[], error=detail, step_id=step_id)
        return {"ok": False, "tool": name, "error": detail, "elapsed_ms": elapsed,
                "failure": {"node": "tool", "kind": "tool_failure", "detail": detail},
                "trace_card": card}


def _emit_call(config: dict | None, agent: str, name: str, args: dict,
               step_id: str = "") -> None:
    from .nodes import _emit_event
    _emit_event(config, "tool_call", {"step_id": step_id, "tool": name, "agent": agent,
                                      "args": {k: str(v)[:80] for k, v in args.items()}})


def _emit_result(config: dict | None, agent: str, name: str, *, ok: bool,
                 elapsed: int, summary: str, refs: list, error: str = "",
                 step_id: str = "") -> None:
    from .nodes import _emit_event
    payload: dict[str, Any] = {"step_id": step_id, "tool": name, "agent": agent, "ok": ok,
                               "summary": summary, "refs": refs, "elapsed_ms": elapsed}
    if error:
        payload["error"] = error
    _emit_event(config, "tool_result", payload)
