"""专家 Agent 节点（多智能体分发，本轮：Reader / Compare）。

- reader_entry / compare_entry：Agent 入口卡 + 范围 Guardrail（交接日志 handoffs 可视化协作）
- align_node：Compare 专属维度对齐（工具白名单内调 align_dimension / get_meta → 对比表 artifact）
- 均为主干图普通节点（不拆编译子图）：recover 回流环与 checkpoint 语义保持不变，
  Agent 区分度由入口卡 / 交接卡 / 专属合成 prompt / 专属 Guardrails 承载
"""
from __future__ import annotations

from .state import AgentState, trace_card
from .tools import call_tool
from ..rag.answering import COMPARE_REFUSAL

FROM_ROUTER = "router"


def _handoff(to: str, note: str) -> dict:
    return {"from": FROM_ROUTER, "to": to, "note": note}


def reader_entry_node(state: AgentState, config=None) -> dict:
    """Reader Agent 入口：范围锁定 Guardrail——强制指定时仅保留首篇选中论文。"""
    agent = state.get("agent", "reader")
    paper_ids = state.get("paper_ids", [])
    forced = state.get("forced_agent", "") not in ("", "auto")
    guardrails: list[str] = []
    updates: dict = {"trace": [], "handoffs": []}

    if forced and len(paper_ids) > 1:
        locked = paper_ids[:1]
        updates["paper_ids"] = locked
        guardrails.append("reader_scope_locked")
        card = trace_card("reader_entry", "Reader Agent 接手",
                          f"范围锁定 · {len(paper_ids)} 篇选中 → 仅精读第 1 篇",
                          {"agent": agent, "locked_paper": locked[0], "scope_lock": True})
        updates["handoffs"].append(
            _handoff("reader", f"强制指定 Reader · 范围锁定为 {locked[0]}"))
    else:
        card = trace_card("reader_entry", "Reader Agent 接手",
                          f"单篇精读 · {len(paper_ids)} 篇在册",
                          {"agent": agent, "scope_lock": False})
        note = ("降级执行 · 单篇精读" if forced else "自动路由 → 单篇精读")
        updates["handoffs"].append(_handoff("reader", note))

    from .nodes import _emit
    _emit(config, card)
    updates["trace"].append(card)
    updates["guardrails"] = guardrails
    return updates


def compare_entry_node(state: AgentState, config=None) -> dict:
    """Compare Agent 入口：单篇拒绝 Guardrail——不足 2 篇时不冒充对比，拒绝并转介。"""
    paper_ids = state.get("paper_ids", [])
    card: dict
    if len(paper_ids) < 2:
        card = trace_card("compare_entry", "Compare Agent · 拒绝",
                          f"范围校验 · 仅 {len(paper_ids)} 篇 → 拒绝对比并转介 Reader",
                          {"agent": "compare", "refused": True, "paper_count": len(paper_ids)})
        from .nodes import _emit
        _emit(config, card)
        return {"mode": "refusal", "answer": COMPARE_REFUSAL.format(n=len(paper_ids)),
                "claims": [],
                "handoffs": [_handoff("compare", f"仅 {len(paper_ids)} 篇 → 拒绝并建议转 Reader")],
                "guardrails": ["compare_scope_refusal"],
                "trace": [card]}
    card = trace_card("compare_entry", "Compare Agent 接手",
                      f"跨文对比 · {len(paper_ids)} 篇 × 全部检索面",
                      {"agent": "compare", "paper_count": len(paper_ids)})
    from .nodes import _emit
    _emit(config, card)
    return {"handoffs": [_handoff("compare", f"跨文对比 · {len(paper_ids)} 篇")],
            "trace": [card]}


def align_node(state: AgentState, config=None) -> dict:
    """Compare 专属：证据池 → 维度对齐对比表（compare_table），并下发 artifact 事件。"""
    from .nodes import _emit, _emit_event
    evidence = state.get("evidence", [])
    paper_ids = list(dict.fromkeys(
        s.get("paper_id", "") for s in state.get("plan", {}).get("steps", [])))
    guardrails: list[str] = []
    failures: list[dict] = []

    titles: dict[str, str] = {}
    for i, pid in enumerate(paper_ids, 1):
        res = call_tool("compare", "get_meta", config,
                        step_id=f"align-meta-{i}", paper_id=pid)
        if res["ok"]:
            titles[pid] = res["data"].get("title", pid)
        else:
            guardrails.append(f"tool_failure:get_meta:{pid[:8]}")
            failures.append(res["failure"])
        if res.get("trace_card"):
            _emit(config, res["trace_card"])

    table: dict = {"papers": paper_ids, "rows": []}
    if evidence:
        res = call_tool("compare", "align_dimension", config,
                        step_id="align-dim", evidence=evidence, paper_ids=paper_ids)
        if res["ok"]:
            table = res["data"]
        else:
            guardrails.append("tool_failure:align_dimension")
            failures.append(res["failure"])
        if res.get("trace_card"):
            _emit(config, res["trace_card"])
    table["titles"] = titles

    found_cells = sum(1 for r in table["rows"] for c in r["cells"] if c.get("found"))
    total_cells = max(1, len(table["rows"]) * max(1, len(paper_ids)))
    card = trace_card("align", "维度对齐",
                      f"{len(table['rows'])} 维 × {len(paper_ids)} 篇 · 命中 {found_cells}/{total_cells} 格",
                      {"rows": len(table["rows"]), "found": found_cells, "total": total_cells})
    _emit(config, card)
    _emit_event(config, "artifact", {"kind": "compare_table", "table": table})
    return {"compare_table": table, "trace": [card],
            "guardrails": guardrails, "failures": failures}


# ---------------- Graph Agent ----------------

def graph_entry_node(state: AgentState, config=None) -> dict:
    """Graph Agent 入口：引用关系问题走元数据证据（references 表），不走 chunk 检索环路。"""
    paper_ids = state.get("paper_ids", [])
    card = trace_card("graph_entry", "Graph Agent 接手",
                      f"引用图谱 · {len(paper_ids)} 篇在册 · 证据=references 元数据",
                      {"agent": "graph", "paper_count": len(paper_ids)})
    from .nodes import _emit
    _emit(config, card)
    return {"handoffs": [_handoff("graph", f"引用图谱分析 · {len(paper_ids)} 篇")],
            "trace": [card]}


def graph_build_node(state: AgentState, config=None) -> dict:
    """Graph 专属：建引用图（真实元数据）→ 计算指标 → 下发 citation_graph artifact。"""
    from .nodes import _emit, _emit_event
    paper_ids = state.get("paper_ids", [])
    guardrails: list[str] = []
    failures: list[dict] = []

    res = call_tool("graph", "citation_graph", config, step_id="graph-build",
                    paper_ids=paper_ids)
    if res.get("trace_card"):
        _emit(config, res["trace_card"])
    if not res["ok"]:
        guardrails.append("tool_failure:citation_graph")
        failures.append(res["failure"])
        graph_data = {"nodes": [], "edges": [], "metrics": {"backend": "none", "nodes": []},
                      "titles": {}}
        card = trace_card("graph_build", "图谱构建", f"失败 · {res['error'][:60]}", {})
        _emit(config, card)
        return {"graph_data": graph_data, "trace": [card],
                "guardrails": guardrails, "failures": failures}
    graph = res["data"]

    metrics_res = call_tool("graph", "graph_metrics", config, step_id="graph-metrics",
                            graph=graph)
    if metrics_res.get("trace_card"):
        _emit(config, metrics_res["trace_card"])
    metrics = metrics_res["data"] if metrics_res["ok"] else {"backend": "none", "nodes": []}
    if not metrics_res["ok"]:
        guardrails.append("tool_failure:graph_metrics")
        failures.append(metrics_res["failure"])

    titles = {n["id"]: n["title"] for n in graph.get("nodes", [])}
    graph_data = {**graph, "metrics": metrics, "titles": titles}
    top = metrics.get("nodes", [])[:3]
    card = trace_card("graph_build", "图谱构建",
                      f"节点 {len(graph['nodes'])} · 引用边 {metrics.get('edge_count', 0)}"
                      f" · 外部引用 {graph.get('external_count', 0)} 条 · 指标 backend={metrics.get('backend')}"
                      + (f" · Top: {top[0]['title'][:14]}…" if top else ""),
                      {"backend": metrics.get("backend"), "top": top})
    _emit(config, card)
    _emit_event(config, "artifact", {"kind": "citation_graph",
                                     "graph": {"nodes": graph.get("nodes", []),
                                               "edges": graph.get("edges", []),
                                               "metrics": metrics}})
    return {"graph_data": graph_data, "trace": [card],
            "guardrails": guardrails, "failures": failures}


# ---------------- Writer Agent ----------------

def writer_entry_node(state: AgentState, config=None) -> dict:
    """Writer Agent 入口：声明 Evidence-First 原则（无证据不开写，校验在 responder）。"""
    card = trace_card("writer_entry", "Writer Agent 接手",
                      "写作模式 · 先检索建证据池，大纲先行、逐节带引用",
                      {"agent": "writer"})
    from .nodes import _emit
    _emit(config, card)
    return {"handoffs": [_handoff("writer", "写作请求 · 先检索后写作")],
            "trace": [card]}


# ---------------- Viz Agent ----------------

VIZ_NO_TABLE = (
    "【Viz Agent · 数据校验】所选论文的解析结果中**没有可图表化的表格数据**，我拒绝凭空出图。\n\n"
    "建议：\n"
    "- 上传/选择含实验结果表的论文（解析后表格会出现在 Document IR 的 tables 中）；\n"
    "- 或改用 **Reader Agent** 就论文文字内容提问。\n"
    "> 我只把真实表格变成图表，不做数据脑补。"
)


def viz_entry_node(state: AgentState, config=None) -> dict:
    """Viz Agent 入口：校验有已解析论文可选，否则拒绝（不冒充出图）。"""
    paper_ids = state.get("paper_ids", [])
    if not paper_ids:
        card = trace_card("viz_entry", "Viz Agent · 拒绝",
                          "未选择论文 → 拒绝出图", {"agent": "viz", "refused": True})
        from .nodes import _emit
        _emit(config, card)
        return {"mode": "refusal",
                "answer": VIZ_NO_TABLE,
                "handoffs": [_handoff("viz", "无选中论文 → 拒绝")],
                "guardrails": ["viz_scope_refusal"], "trace": [card]}
    card = trace_card("viz_entry", "Viz Agent 接手",
                      f"数据出图 · {len(paper_ids)} 篇 · 只走真实表格（声明式 spec，无代码执行）",
                      {"agent": "viz", "paper_count": len(paper_ids)})
    from .nodes import _emit
    _emit(config, card)
    return {"handoffs": [_handoff("viz", f"出图请求 · {len(paper_ids)} 篇")],
            "trace": [card]}


def viz_extract_node(state: AgentState, config=None) -> dict:
    """Viz 专属（不走全文检索管线）：抽表 → chart_spec（白名单变换）→ artifact。

    无表/表不可图表化 → refusal（responder 透传）；默认取第一张可出图表格，
    数值列自动探测（从第 2 列起找首个全数值列）。
    """
    from .nodes import _emit, _emit_event
    paper_ids = state.get("paper_ids", [])
    all_tables: list[dict] = []
    failures: list[dict] = []
    for i, pid in enumerate(paper_ids, 1):
        res = call_tool("viz", "extract_table", config, step_id=f"viz-table-{i}", paper_id=pid)
        if res.get("trace_card"):
            _emit(config, res["trace_card"])
        if res["ok"]:
            all_tables.extend(res["data"].get("tables", []))
        else:
            failures.append(res["failure"])
        if res["ok"] and not res["data"].get("parsed", True):
            failures.append({"node": "viz_extract", "kind": "tool_failure",
                             "detail": f"paper {pid} 未解析"})

    for t in all_tables:
        t.setdefault("paper_id", paper_ids[0] if paper_ids else "")

    if not all_tables:
        card = trace_card("viz_extract", "表格抽取", "0 张表 → 拒绝出图", {"tables": 0})
        _emit(config, card)
        return {"mode": "refusal", "answer": VIZ_NO_TABLE,
                "guardrails": ["viz_no_tables"],
                "failures": failures, "trace": [card]}

    spec_res: dict | None = None
    used_table: dict | None = None
    reasons: list[str] = []
    for t in all_tables:
        # 数值列探测：默认 y=1；若该列非数值，逐列尝试（上限 4 列）
        for y in range(1, min(4, max(2, len(t.get("headers") or [])))):
            r = call_tool("viz", "chart_spec_builder", config, step_id="viz-spec",
                          table=t, x_col=0, y_col=y, chart_type="bar")
            if r.get("trace_card"):
                _emit(config, r["trace_card"])
            if r["ok"] and isinstance(r["data"], dict) and r["data"].get("valid"):
                spec_res = r["data"]
                used_table = t
                break
            reasons.append(str((r.get("data") or {}).get("reason") or r.get("error", ""))[:60])
        if spec_res:
            break

    if not spec_res:
        card = trace_card("viz_extract", "表格变换",
                          f"{len(all_tables)} 张表均无可解析数值列 → 拒绝出图",
                          {"tables": len(all_tables), "reasons": reasons[:3]})
        _emit(config, card)
        return {"mode": "refusal", "answer": VIZ_NO_TABLE,
                "guardrails": ["viz_no_numeric_columns"],
                "failures": failures, "trace": [card]}

    spec = spec_res["spec"]
    prov = spec_res.get("provenance") or {}
    card = trace_card("viz_extract", "图表生成",
                      f"{spec['type']} 图 · {len(spec['data'])} 个数据点 · 来源 p{prov.get('page', '?')} 表{prov.get('table_id', '')}",
                      {"chart_type": spec["type"], "points": len(spec["data"]),
                       "provenance": prov})
    _emit(config, card)
    _emit_event(config, "artifact", {"kind": "chart_spec", "spec": spec,
                                     "provenance": prov})
    return {"chart_spec": {**spec, "provenance": prov},
            "guardrails": (["viz_used_fallback_column"] if len(reasons) else []),
            "failures": failures, "trace": [card]}
