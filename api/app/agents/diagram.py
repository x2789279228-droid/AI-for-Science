"""拓扑图脚本生成（模块六 P0.5）：LLM 产 DiagramSpec → 确定性渲染四格式。

防幻觉/防注入设计：LLM 只输出结构化 {nodes, edges}（pydantic 校验 + 引用 id
存在性检查），四种脚本由代码从 spec 确定性生成——不执行任意用户代码；matplotlib
走现有 python_sandbox（-I 隔离 + 30s 超时）渲染 PNG。
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from ..llm import client as llm
from ..llm.prompts import resolve as _resolve
from ..log import logger

_MAX_NODES = 12


class DiagNode(BaseModel):
    id: str
    label: str = ""


class DiagEdge(BaseModel):
    from_: str = Field(alias="from")
    to: str
    label: str = ""

    model_config = {"populate_by_name": True}


class DiagramSpec(BaseModel):
    nodes: list[DiagNode] = Field(default_factory=list)
    edges: list[DiagEdge] = Field(default_factory=list)


def spec_from_description(description: str) -> DiagramSpec:
    """描述 → DiagramSpec（LLM 结构化；校验 id 引用存在、节点数上限）。

    LLM 不可用/调用失败时抛异常，由 generate_diagram 捕获后走 _rule_spec 规则降级
    （Round7：旧实现直接 RuntimeError(llm_unavailable) 冒到路由 → 503，e2e ⑩ 失败）。"""
    if not llm.available():
        raise RuntimeError("llm_unavailable:LLM 不可用")
    system, pv = _resolve("copilot", "system_diagram_spec",
                          "你是架构图结构化器。只输出 DiagramSpec JSON。")
    res = llm.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": description[:1500]}],
        json_mode=True, temperature=0.0, max_tokens=800, module="diagram_spec",
        audit_prompt=description[:150], prompt_version=pv)
    data = llm.parse_json(res.get("text", "")) or {}
    spec = DiagramSpec.model_validate(data)
    ids = {n.id for n in spec.nodes}
    if not ids:
        raise ValueError("empty_nodes")
    if len(spec.nodes) > _MAX_NODES:
        spec.nodes = spec.nodes[:_MAX_NODES]
    spec.edges = [e for e in spec.edges if e.from_ in ids and e.to in ids]
    return spec


# ---------- 无 LLM 规则降级（确定性，不冒充 LLM） ----------

_RULE_ARROW = re.compile(r"-{1,2}>|=>|→|➜|➔")
_RULE_STEP_SPLIT = re.compile(r"[，,、；;。!?！？\n]+")
_RULE_ENUM = re.compile(r"^\s*(?:第?\s*\d+\s*[步.、):：）]|\d+\s*[.)、]|步骤\s*\d+\s*[:：])\s*")


def _clean_step_label(raw: str) -> str:
    """步骤标签清洗：去序号前缀、合并空白；「系统名：步骤」保留步骤本体；截 40 字。"""
    s = re.sub(r"\s+", " ", (raw or "").strip())
    s = _RULE_ENUM.sub("", s)
    m = re.match(r"^([^：:]{1,14})[：:]\s*(.{2,})$", s)
    if m:
        s = m.group(2)          # 形如「RAG 系统：Query 改写器」→ 节点名取后半
    return s[:40].strip()


def _rule_spec(description: str) -> DiagramSpec:
    """规则解析：按 →/->/=> 拆链生成节点+边；无箭头时按句读/逗号拆步；≤12 节点。

    这是 LLM 不可用/失败时的确定性降级路径，保证拓扑图功能不 503、四格式脚本
    仍可生成（结果带 guardrail=diagram_rule_fallback 如实标注「非 LLM 解析」）。"""
    text = (description or "").strip()
    if not text:
        raise ValueError("description 为空")
    steps: list[str] = []
    if _RULE_ARROW.search(text):
        segments = _RULE_ARROW.split(text)
        for seg in segments:
            steps.extend(p for p in _RULE_STEP_SPLIT.split(seg) if p.strip())
    else:
        steps = [p for p in _RULE_STEP_SPLIT.split(text) if p.strip()]
    labels: list[str] = []
    for raw in steps:
        lab = _clean_step_label(raw)
        if lab and (not labels or labels[-1] != lab):
            labels.append(lab)
        if len(labels) >= _MAX_NODES:
            break
    if not labels:
        raise ValueError("empty_nodes:未能从描述中解析出节点")
    nodes = [DiagNode(id=f"n{i}", label=lab) for i, lab in enumerate(labels, 1)]
    edges = [DiagEdge.model_validate({"from": nodes[i].id, "to": nodes[i + 1].id})
             for i in range(len(nodes) - 1)]
    return DiagramSpec(nodes=nodes, edges=edges)


def _safe(s: str) -> str:
    return (s or "").replace('"', "'").replace("\n", " ")[:40]


def render_mermaid(spec: DiagramSpec) -> str:
    lines = ["graph TD"]
    for n in spec.nodes:
        lines.append(f'    {n.id}["{_safe(n.label or n.id)}"]')
    for e in spec.edges:
        tag = f'|{_safe(e.label)}|' if e.label else ""
        lines.append(f"    {e.from_} -->{tag} {e.to}")
    return "\n".join(lines)


def render_dot(spec: DiagramSpec) -> str:
    lines = ["digraph G {", "  rankdir=TB;", "  node [shape=box, style=rounded, fontname=\"Helvetica\"];"]
    for n in spec.nodes:
        lines.append(f'  "{n.id}" [label="{_safe(n.label or n.id)}"];')
    for e in spec.edges:
        attr = f' [label="{_safe(e.label)}"]' if e.label else ""
        lines.append(f'  "{e.from_}" -> "{e.to}"{attr};')
    lines.append("}")
    return "\n".join(lines)


def render_tikz(spec: DiagramSpec) -> str:
    """TikZ standalone 源码（nodes 网格布局 + arrows；标签转义 &/{}/）。"""
    def esc(s: str) -> str:
        for ch in ("&", "%", "$", "#", "_", "{", "}"):
            s = s.replace(ch, "\\" + ch)
        return s

    import math
    cols = math.ceil(math.sqrt(len(spec.nodes)))
    pos = {n.id: ((i % cols) * 2.6, -(i // cols) * 1.6) for i, n in enumerate(spec.nodes)}
    lines = ["\\documentclass[tikz,border=6pt]{standalone}",
             "\\usetikzlibrary{arrows.meta}", "\\begin{document}",
             "\\begin{tikzpicture}[node distance=18mm,",
             "  box/.style={draw, rounded corners, align=center, minimum width=22mm, font=\\small}]"]
    for n in spec.nodes:
        x, y = pos[n.id]
        lines.append(f'  \\node[box] ({n.id}) at ({x:.1f},{y:.1f}) {{{esc(_safe(n.label or n.id))}}};')
    for e in spec.edges:
        label = f' [midway, above, font=\\scriptsize] node {{{esc(_safe(e.label))}}}' if e.label else ""
        lines.append(f'  \\draw[-{{Stealth}}] ({e.from_}) -- ({e.to}){label};')
    lines += ["\\end{tikzpicture}", "\\end{document}"]
    return "\n".join(lines)


def render_matplotlib_png(spec: DiagramSpec) -> dict:
    """经 python_sandbox 渲染拓扑 PNG（复用现有 -I 隔离管线与出图收编）。"""
    from .tools import _python_sandbox
    code = (
        "import matplotlib\n"
        "import matplotlib.pyplot as plt\n"
        "import networkx as nx\n"
        f"g = nx.DiGraph()\n"
        f"g.add_nodes_from({[n.id for n in spec.nodes]!r})\n"
        f"g.add_edges_from({[(e.from_, e.to) for e in spec.edges]!r})\n"
        "pos = nx.spring_layout(g, seed=42, k=1.4)\n"
        "fig, ax = plt.subplots(figsize=(9, 6))\n"
        "nx.draw_networkx_edges(g, pos, ax=ax, arrows=True, arrowsize=18, "
        "edge_color='#64748b', connectionstyle='arc3,rad=0.08')\n"
        "nx.draw_networkx_nodes(g, pos, ax=ax, node_color='#93c5fd', "
        "node_size=2200, edgecolors='#1d4ed8')\n"
        f"labels = { {n.id: (n.label or n.id)[:14] for n in spec.nodes}!r }\n"
        "nx.draw_networkx_labels(g, pos, labels, ax=ax, font_size=9)\n"
        "ax.set_axis_off()\n"
        "fig.tight_layout()\n"
        "fig.savefig('figure.png', dpi=160)\n"
    )
    out = _python_sandbox(code)
    if out.get("ok") and out.get("image_path"):
        return {"ok": True, "image_url": out["image_path"],
                "stderr": (out.get("stderr") or "")[-300:]}
    logger.warning(f"diagram matplotlib 渲染失败: {out.get('stderr', '')[:200]}",
                   module="diagram")
    return {"ok": False, "error": (out.get("stderr") or "sandbox_failed")[:300]}


_MERMAID_HEAD = re.compile(r"^\s*(graph|flowchart)\s+\w+", re.IGNORECASE)
# 边语法：--> / -> / -.-> / ==>（renderer 恒用箭头式；`-->|标签|` 变体由前缀覆盖）
_MERMAID_EDGE = re.compile(r"-\.->|==>|-{1,2}>")
# 节点声明：`id["label"]` / `id[label]`（单节点图合法：只有节点、没有边）
_MERMAID_NODE = re.compile(r"^\s*[\w.-]+\[", re.MULTILINE)


def validate_syntax(kind: str, source: str) -> dict:
    """脚本语法健全性检查（本地确定性，不装外部二进制）：
    mermaid/dot/tikz 结构配平；matplotlib 由 sandbox 渲染结果判定。

    Round4 修复：mermaid 旧式 `startswith("graph") and "->" in s or "-->" in s`
    因 and 优先于 or，任何含 "-->" 的字符串都通过（无 graph 前缀也过）——评审
    指出的优先级 bug；同时补 flowchart 前缀（mermaid 官方两种等价写法）与
    无箭头连线 `--`/带标签边。
    Round7：允许「只有节点声明、没有边」的单节点图（规则降级可能出现单步描述）。"""
    if kind == "mermaid":
        s = source.strip()
        ok = bool(_MERMAID_HEAD.match(s)
                  and (_MERMAID_EDGE.search(s) or _MERMAID_NODE.search(s)))
        return {"ok": ok, "checker": "structural"}
    if kind == "dot":
        bal = source.count("{") == source.count("}") and source.count('"') % 2 == 0
        ok = source.strip().startswith("digraph") and bal
        return {"ok": bool(ok), "checker": "structural+balance"}
    if kind == "tikz":
        bal = source.count("\\begin{") == source.count("\\end{") and source.count("{") == source.count("}")
        nodes_n = source.count("\\node[")
        ok = bal and nodes_n > 0
        return {"ok": bool(ok), "checker": "balance+nodecount", "nodes": nodes_n}
    return {"ok": False, "checker": "unknown"}


def generate_diagram(description: str, fmt: str = "all") -> dict:
    """主入口：spec → {spec, sources{mermaid,dot,tikz}, matplotlib_png, validation}。

    LLM 不可用/解析失败 → 规则降级（不 503），结果附 guardrail=diagram_rule_fallback。
    """
    fallback = False
    try:
        spec = spec_from_description(description)
    except Exception as exc:  # noqa: BLE001 —— LLM 不可用/解析失败都必须降级而非报错
        logger.warning(f"diagram LLM 解析不可用，规则降级: {type(exc).__name__}: {exc}",
                       module="diagram")
        spec = _rule_spec(description)
        fallback = True
    sources = {"mermaid": render_mermaid(spec), "dot": render_dot(spec),
               "tikz": render_tikz(spec)}
    validation = {k: validate_syntax(k, v) for k, v in sources.items()}
    png = render_matplotlib_png(spec) if fmt in ("all", "matplotlib") else {}
    validation["matplotlib"] = {"ok": bool(png.get("ok")),
                                "checker": "sandbox_render"} if png else {"ok": None}
    out = {"spec": json.loads(spec.model_dump_json(by_alias=True)), "sources": sources,
           "matplotlib": png, "validation": validation}
    if fallback:
        out["guardrail"] = "diagram_rule_fallback"
    return out
