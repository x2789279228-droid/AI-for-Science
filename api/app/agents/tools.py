"""ToolSpec 工具注册表：Agent 能力的统一暴露面（Function Calling 证据链）。

- 每个 Agent 只能调用 allowed_agents 白名单内的工具（越权 → ToolDenied，计入 failures）
- call_tool 统一计时、发 tool_call/tool_result 协议事件（v1.1）与 trace 卡，
  与 retrieve_task 内联版本形状一致；业务级失败交由调用方决策（recover 策略②）
- 工具实现皆为纯函数（读库/读索引/纯变换），可单独单测
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .. import db, jobs
from ..config import DATA_DIR, settings
from ..rag import indexing
from ..translate import cached_translations as _cached_translations_impl
from ..translate import translate_blocks as _translate_blocks_impl
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
    # 只读/幂等工具失败自动重试 1 次（瞬时网络故障）；有副作用的工具不重试
    retryable: bool = False


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
                    "chunk_id": h.get("chunk_id", ""),
                    "page": (h.get("page") or 0) + 1,  # SSE 对外 1 基人读页码
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
        "SELECT id,title,arxiv_id FROM papers WHERE status= 'active'") if r["id"]}
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

from ..parsing.tables import parse_md_table as _parse_md_table


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
            "provenance": {"paper_id": table.get("paper_id"),
                           "page": (table.get("page") or 0) + 1,  # 对外 1 基人读页码
                           "table_id": table.get("id"), "skipped_rows": skipped}}


# ---- 4.4 新增工具：arXiv/下载/解析/复现性/沙箱/翻译 ----

def _tool_cache_read(name: str, key: str) -> dict | None:
    """离线回放缓存：上次成功结果存 data/tool_cache/，断网演示时兜底。"""
    path = DATA_DIR / "tool_cache" / f"{name}_{hashlib.sha1(key.encode()).hexdigest()[:12]}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 缓存损坏视同无缓存
            return None
    return None


def _tool_cache_write(name: str, key: str, payload: dict) -> None:
    try:
        path = DATA_DIR / "tool_cache" / f"{name}_{hashlib.sha1(key.encode()).hexdigest()[:12]}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _arxiv_search(query: str, max_results: int = 8) -> dict:
    """arXiv 检索（export.arxiv.org Atom API，无需 key），按提交时间倒序。

    断网兜底：网络失败时回放 data/tool_cache/ 上次成功结果（source=cache_offline 如实标注）。
    """
    import xml.etree.ElementTree as ET

    ns = {"a": "http://www.w3.org/2005/Atom"}
    q = query.strip()
    try:
        resp = httpx.get(
            "http://export.arxiv.org/api/query",
            params={"search_query": f"all:{q}", "start": 0,
                    "max_results": max(1, min(int(max_results), 20)),
                    "sortBy": "submittedDate", "sortOrder": "descending"},
            headers={"User-Agent": "meridian-scholar-agent/1.0"}, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        entries = []
        for e in root.findall("a:entry", ns):
            aid = (e.findtext("a:id", "", ns) or "").rsplit("/abs/", 1)[-1]
            entries.append({
                "arxiv_id": aid,
                "title": " ".join((e.findtext("a:title", "", ns) or "").split()),
                "authors": [x.findtext("a:name", "", ns) for x in e.findall("a:author", ns)][:6],
                "abstract": " ".join((e.findtext("a:summary", "", ns) or "").split())[:400],
                "pdf_url": (e.findtext("a:id", "", ns) or "").replace("/abs/", "/pdf/") + ".pdf",
                "published": (e.findtext("a:published", "", ns) or "")[:10],
            })
        _tool_cache_write("arxiv", q, {"query": q, "entries": entries})
        return {"ok": True, "query": q, "entries": entries, "source": "live"}
    except Exception as e:  # noqa: BLE001 —— 网络失败回放缓存，演示不中断
        cached = _tool_cache_read("arxiv", q)
        if cached:
            return {"ok": True, "query": q, "entries": cached.get("entries", []),
                    "source": "cache_offline", "live_error": f"{type(e).__name__}: {e}"[:120]}
        return {"ok": False, "query": q, "entries": [], "source": "none",
                "error": f"{type(e).__name__}: {e}"[:120]}


def _paper_download(arxiv_id: str = "", pdf_url: str = "") -> dict:
    """下载 arXiv PDF（主源 arxiv.org，备源 export.arxiv.org）→ 复用 upload_pdf 入库+解析。"""
    if not arxiv_id and not pdf_url:
        return {"ok": False, "error": "arxiv_id_or_pdf_url_required"}
    aid = (arxiv_id or "").strip()
    sources = ([f"https://arxiv.org/pdf/{aid}", f"https://export.arxiv.org/pdf/{aid}"]
               if aid else [pdf_url])
    last_err = ""
    for src in sources:
        try:
            with httpx.stream("GET", src, follow_redirects=True, timeout=180) as resp:
                resp.raise_for_status()
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > 100 * 1024 * 1024:
                        return {"ok": False, "error": "pdf_too_large_over_100mb"}
            if len(buf) < 1024 or bytes(buf[:5]) != b"%PDF-":
                last_err = f"not_a_pdf:{src}"
                continue
            out = jobs.upload_pdf(f"{aid or 'arxiv-download'}.pdf", bytes(buf))
            return {"ok": True, "source": src, "bytes": len(buf), **out}
        except Exception as e:  # noqa: BLE001 —— 主源失败换备源
            last_err = f"{type(e).__name__}: {e}"
    return {"ok": False, "error": f"download_failed:{last_err}"[:160]}


def _pdf_parse(paper_id: str, wait_s: int = 90) -> dict:
    """触发解析并等待（评分表"异步 task_id"两种形态都给：等完返回结果 / 超时返回 job_id）。"""
    rows = db.query("SELECT docir_path FROM papers WHERE id=?", (paper_id,))
    if rows and rows[0]["docir_path"]:
        return {"ok": True, "status": "already_parsed", "paper_id": paper_id}
    job_id = jobs.submit_parse(paper_id)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        j = db.get_job(job_id)
        if j and j["status"] in ("done", "error"):
            return {"ok": j["status"] == "done", "job_id": job_id, "status": j["status"],
                    "progress": j["progress"], "message": j["message"]}
        time.sleep(1.0)
    return {"ok": True, "job_id": job_id, "status": "running",
            "message": "解析仍在进行，job_id 可轮询 /api/jobs/{id}"}


def _repro_score(repo: dict) -> float:
    """复现性分 = 0.4*Stars 档位 + 0.4*活跃度 + 0.2*许可证宽松度。"""
    stars = int(repo.get("stars") or 0)
    stars_tier = 1.0 if stars >= 1000 else 0.6 if stars >= 100 else 0.3 if stars >= 10 else 0.0
    pushed = repo.get("pushed_at") or ""
    try:
        dt = datetime.fromisoformat(pushed) if pushed else None  # fromisoformat 兼容 Z 后缀
        days = (datetime.now(dt.tzinfo) - dt).days if dt else 9999
    except ValueError:
        days = 9999
    activity = 1.0 if days <= 90 else 0.6 if days <= 365 else 0.0
    lic = 1.0 if repo.get("license") in ("MIT", "Apache-2.0", "BSD-2-Clause",
                                         "BSD-3-Clause") else 0.0
    return round(0.4 * stars_tier + 0.4 * activity + 0.2 * lic, 2)


def _github_repro(title: str) -> dict:
    """GitHub 仓库搜索 + 复现性打分（可选 GITHUB_TOKEN 提额；无 token 限流如实降级）。"""
    q = "+".join(re.sub(r"[^A-Za-z0-9 ]", " ", title).split()[:6])
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "meridian-scholar-agent"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    try:
        r = httpx.get("https://api.github.com/search/repositories",
                      params={"q": q, "sort": "stars", "per_page": 3},
                      headers=headers, timeout=20)
        r.raise_for_status()
        repos = [{"repo": it["full_name"], "stars": it.get("stargazers_count", 0),
                  "pushed_at": (it.get("pushed_at") or "")[:10],
                  "license": (it.get("license") or {}).get("spdx_id", ""),
                  "url": it.get("html_url", "")} for it in r.json().get("items", [])[:3]]
        for it in repos:
            it["repro_score"] = _repro_score(it)
        _tool_cache_write("github", q, {"query": q, "repos": repos})
        return {"ok": True, "query": q, "repos": repos, "source": "live"}
    except Exception as e:  # noqa: BLE001
        cached = _tool_cache_read("github", q)
        if cached:
            return {"ok": True, "query": q, "repos": cached.get("repos", []),
                    "source": "cache_offline", "live_error": f"{type(e).__name__}: {e}"[:120]}
        return {"ok": False, "query": q, "repos": [], "error": f"{type(e).__name__}: {e}"[:120]}


_SANDBOX_PREAMBLE = (
    # matplotlib 为可选依赖（pip 组 sandbox）：未安装时非绘图脚本仍可执行
    "try:\n"
    "    import matplotlib\n"
    "    matplotlib.use('Agg')\n"
    "    import matplotlib.pyplot as plt\n"
    "except ImportError:\n"
    "    pass\n"
    "FIGURE_OUT = r'{fig}'\n"
)


def _python_sandbox(code: str) -> dict:
    """受限 Python 执行：独立子进程 `-I`（隔离 sys.path/环境变量）+ 30s 超时 + 临时目录。

    matplotlib 预置 Agg 离线渲染；脚本结束时若存在 figure.png 自动收编到
    data/figures/sandbox/。网络隔离口径见实现方案文档（演示环境无外网依赖，
    子进程隔离 + 超时 + 只写临时目录为本实现的三道硬防线）。
    """
    tmp = Path(tempfile.mkdtemp(prefix="sbx_"))
    fig = tmp / "figure.png"
    script = tmp / "main.py"
    script.write_text(_SANDBOX_PREAMBLE.format(fig=fig) + code, encoding="utf-8")
    try:
        proc = subprocess.run([sys.executable, "-I", str(script)], check=False,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30, cwd=str(tmp))
        image_path = None
        if fig.exists():
            dest = DATA_DIR / "figures" / "sandbox" / \
                f"{hashlib.sha1(code.encode()).hexdigest()[:12]}.png"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fig, dest)
            image_path = f"/api/sandbox/figure/{dest.name}"
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "stdout": proc.stdout[:4000], "stderr": proc.stderr[:2000],
                "image_path": image_path}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "sandbox_timeout_30s", "exit_code": None,
                "stdout": "", "stderr": "", "image_path": None}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _translate_block(paper_id: str, block_id: str, lang: str = "zh") -> dict:
    """单块翻译（4.7 translate.py 地基的 Agent 侧薄封装，缓存复用）。"""
    stats = _translate_blocks_impl(paper_id, [block_id], lang=lang)
    cached = _cached_translations_impl(paper_id, lang).get(block_id)
    return {"ok": stats["failed"] == 0, "paper_id": paper_id, "block_id": block_id,
            "translation": cached, "stats": stats}


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
    # ---- 4.4 新增（评分表 8 工具齐编 + 白名单矩阵扩展）----
    ToolSpec(
        name="arxiv_search", desc="arXiv 论文检索（Atom API，按提交时间倒序，断网回放缓存）",
        params={"query": "str", "max_results": "int"},
        allowed_agents={"reader", "compare", "writer", "explore"}, fn=_arxiv_search,
        returns='{"ok","entries":[{arxiv_id,title,authors,abstract,pdf_url,published}],"source"}',
        retryable=True),
    ToolSpec(
        name="paper_download", desc="下载 arXiv PDF 入库（主源+备源，>100MB 拒绝，复用精确去重）",
        params={"arxiv_id": "str", "pdf_url": "str"},
        allowed_agents={"explore"}, fn=_paper_download,
        returns='{"ok","paper_id","job_id","bytes","source"}'),
    ToolSpec(
        name="pdf_parse", desc="触发解析并等待（或超时返回 job_id 交前端进度条）",
        params={"paper_id": "str", "wait_s": "int"},
        allowed_agents={"explore"}, fn=_pdf_parse,
        returns='{"ok","job_id","status","progress","message"}'),
    ToolSpec(
        name="github_repro", desc="GitHub 仓库搜索 + 复现性分（stars/活跃度/许可证加权）",
        params={"title": "str"},
        allowed_agents={"reader", "explore"}, fn=_github_repro,
        returns='{"ok","repos":[{repo,stars,pushed_at,license,repro_score}],"source"}',
        retryable=True),
    ToolSpec(
        name="python_sandbox", desc="受限 Python 执行（子进程 -I 隔离 + 30s 超时 + Agg 离线出图）",
        params={"code": "str"},
        allowed_agents={"reader", "writer"}, fn=_python_sandbox,
        returns='{"ok","exit_code","stdout","stderr","image_path"}'),
    ToolSpec(
        name="translate_block", desc="翻译指定论文的单个 DocIR 块（占位符保护 + 术语表 + 缓存）",
        params={"paper_id": "str", "block_id": "str", "lang": "str"},
        allowed_agents={"reader", "writer"}, fn=_translate_block,
        returns='{"ok","translation":{text,format_ok},"stats"}'),
]}


def assert_allowed(agent: str, tool: str) -> None:
    spec = TOOLS.get(tool)
    if spec is None:
        raise ToolDenied(f"unknown_tool:{tool}")
    if agent not in spec.allowed_agents:
        raise ToolDenied(f"agent '{agent}' not allowed for tool '{tool}'"
                         f" (allowed: {sorted(spec.allowed_agents)})")


def call_tool(agent: str, name: str, config: dict | None, *,
              step_id: str = "", retries: int | None = None, **args) -> dict:
    """统一工具调用入口：白名单校验 → 计时 → tool_call/tool_result 事件 + trace 卡。

    step_id 用于前端 Trace 时间线点亮（计划内步骤传 plan 分配的 id，计划外传
    "align:*" 等自描述 id，TracePanel 以独立工具行渲染）。
    失败重试（评分表"失败重试"）：只读/幂等工具（spec.retryable）默认自动重试 1 次；
    有副作用工具默认不重试，可显式传 retries 覆盖。失败详情注明"已重试"。
    返回 {"ok": bool, "tool": name, "data"|"error", "elapsed_ms", "failure"|"trace_card"}；
    业务异常不上抛——调用方拿 failure 自行决策（计入 failures / 交 recover）。
    """
    t0 = time.time()
    _emit_call(config, agent, name, args, step_id)
    spec = TOOLS.get(name)
    n_tries = (retries if retries is not None else 0) + 1
    if retries is None and spec is not None and spec.retryable:
        n_tries = 2
    retried = False
    try:
        assert_allowed(agent, name)
        data: Any = None
        last_exc: Exception | None = None
        for attempt in range(n_tries):
            try:
                data = TOOLS[name].fn(**args)
                last_exc = None
                break
            except Exception as e:  # noqa: BLE001
                last_exc = e
                retried = attempt > 0   # 只有真正发起过第二次尝试才标"已重试"
        if last_exc is not None:
            raise last_exc
        elapsed = int((time.time() - t0) * 1000)
        card = trace_card("tool", f"工具·{name}",
                          f"{agent} 调用成功 · {elapsed}ms", {"tool": name, "agent": agent})
        _emit_result(config, agent, name, ok=True, elapsed=elapsed,
                     summary=card["summary"], refs=[], step_id=step_id)
        return {"ok": True, "tool": name, "data": data, "elapsed_ms": elapsed,
                "trace_card": card}
    except Exception as e:  # noqa: BLE001 —— 失败也是证据：计入 failures 供 recover 决策
        elapsed = int((time.time() - t0) * 1000)
        suffix = "（已自动重试仍失败）" if retried else ""
        detail = f"{type(e).__name__}: {e}"[:120]
        card = trace_card("tool", f"工具·{name}", f"{agent} 调用失败{suffix} · {detail}",
                          {"tool": name, "agent": agent, "error": detail,
                           "retried": retried})
        _emit_result(config, agent, name, ok=False, elapsed=elapsed, summary=card["summary"],
                     refs=[], error=detail + suffix, step_id=step_id)
        return {"ok": False, "tool": name, "error": detail + suffix, "elapsed_ms": elapsed,
                "failure": {"node": "tool", "kind": "tool_failure",
                            "detail": detail + suffix, "retried": retried},
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
