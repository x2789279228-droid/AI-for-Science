"""Discover 闭环端点（4.4 加分主菜）：搜索 → 勾选 → 下载 → 解析 → 检索问答。

确定性流水编排（不依赖 LLM 规划工具调用，演示稳）：SSE 事件协议与 chat 一致
（tool_call/tool_result），前端可复用时间线组件。断网演示靠 tool_cache 回放。
"""
from __future__ import annotations

import json
import queue as queue_mod
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..agents.tools import call_tool
from ..config import DATA_DIR
from ..llm import client as llm
from ..llm.prompts import prompt_version
from ..rag import indexing

router = APIRouter(prefix="/api")


class DiscoverBody(BaseModel):
    query: str
    auto: bool = False          # 自动下载 top1 并等解析（一次闭环形态）
    max_results: int = 6


class MultiSearchBody(BaseModel):
    """模块四 P0-1 多源检索：四选一入口（query/title/doi|arxiv_id/references_text）。"""
    query: str = ""             # 方向关键词（多源模糊检索）
    title: str = ""             # 论文标题（精确解析）
    doi: str = ""
    arxiv_id: str = ""
    references_text: str = ""   # 参考文献列表（BibTeX/RIS/纯文本）
    sources: list[str] | None = None   # 默认 ["arxiv","s2","openalex"]
    max_results: int = 8


class DownloadBody(BaseModel):
    arxiv_id: str = ""
    pdf_url: str = ""           # P0-1：非 arXiv 论文的开放 PDF 直链（二选一）


@router.post("/discover")
def discover(body: DiscoverBody):
    if not body.query.strip():
        raise HTTPException(400, "query 为空")

    q: queue_mod.Queue = queue_mod.Queue()

    def worker() -> None:
        try:
            search_q = body.query.strip()
            # ① LLM 抽检索词（英文术语面更好命中；失败规则回退原文）
            if llm.available():
                pv = prompt_version("translate", "system_translate")  # 占位：无专用 yaml 槽位
                res = llm.chat(
                    [{"role": "system", "content":
                        "你是检索词改写器。把用户的研究方向改写为适合 arXiv 全文检索的英文检索词"
                        "（保留领域术语，去掉口头语）。"
                        '输出 JSON：{"query":"english search terms"}。'},
                     {"role": "user", "content": body.query}],
                    json_mode=True, temperature=0.0, module="discover",
                    audit_prompt=body.query[:200], prompt_version=pv)
                data = llm.parse_json(res.get("text", ""))
                if isinstance(data, dict) and data.get("query"):
                    search_q = str(data["query"])
            q.put(("status", {"stage": "search", "query": search_q, "note":
                              "arXiv 检索（按提交时间倒序）"}))
            # ② arXiv 检索
            r = call_tool("explore", "arxiv_search", None, step_id="discover-search",
                          query=search_q, max_results=body.max_results)
            entries = (r.get("data") or {}).get("entries", []) if r.get("ok") else []
            source = (r.get("data") or {}).get("source", "none")
            q.put(("discover_results", {"query": search_q, "entries": entries,
                                        "source": source}))
            # ③ auto 闭环：下载 top1 → 等解析 → kb_search 证据
            if body.auto and entries:
                top = entries[0]
                q.put(("status", {"stage": "download", "arxiv_id": top["arxiv_id"],
                                  "note": f"自动下载 top1：{top['title'][:60]}"}))
                d = call_tool("explore", "paper_download", None, step_id="discover-download",
                              arxiv_id=top["arxiv_id"])
                if not d.get("ok"):
                    q.put(("error", {"message": f"下载失败：{d.get('error')}"}))
                    return
                paper_id = d["data"]["paper_id"]
                q.put(("status", {"stage": "parse", "paper_id": paper_id,
                                  "note": "解析中（MinerU/PyMuPDF 路由）"}))
                p = call_tool("explore", "pdf_parse", None, step_id="discover-parse",
                              paper_id=paper_id, wait_s=180)
                if not p.get("ok") or p["data"].get("status") == "error":
                    q.put(("error", {"message": f"解析失败：{p.get('data', {}).get('message') or p.get('error')}"}))
                    return
                hits = indexing.hybrid_search([paper_id], search_q, top_k=5)
                q.put(("final", {"paper_id": paper_id, "job_id": d["data"].get("job_id"),
                                 "hits": [{"chunk_id": h.get("chunk_id"),
                                           "page": (h.get("page") or 0) + 1,
                                           "text": (h.get("text") or "")[:300]}
                                          for h in hits.get("hits", [])],
                                 "note": "闭环完成：搜索→下载→解析→检索"}))
            else:
                q.put(("final", {"entries": len(entries), "note":
                                 "勾选论文后点「下载并解析」完成闭环"}))
        except Exception as e:  # noqa: BLE001 —— 编排失败必须可观测
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            q.put(("done", {}))

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            try:
                kind, data = q.get(timeout=600)
            except queue_mod.Empty:
                yield 'event: error\ndata: {"message":"discover timeout"}\n\n'
                return
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if kind == "done":
                return

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/discover/search")
def discover_search(body: MultiSearchBody):
    """多源检索（SSE）：status（逐源）→ discover_results（去重合并候选）→ final。

    入口四选一：方向关键词（arXiv+S2+OpenAlex 模糊）/ 标题（S2 精确，回退 OpenAlex）/
    DOI/arXiv id（S2 精确）/ 参考文献列表（BibTeX/RIS/纯文本解析 → S2 batch 补全，
    未解析条目如实标 resolved=false）。断网：各源独立回放 tool_cache。
    """
    from ..rag import discovery

    if not any((body.query, body.title, body.doi, body.arxiv_id, body.references_text)):
        raise HTTPException(400, "query/title/doi/arxiv_id/references_text 至少一个")
    q: queue_mod.Queue = queue_mod.Queue()

    def worker() -> None:
        try:
            if body.references_text:
                q.put(("status", {"stage": "parse_refs", "note": "解析参考文献列表"}))
                parsed = discovery.parse_reference_list(body.references_text)
                q.put(("status", {"stage": "resolve", "note":
                                  f"{parsed['kind']} 格式 {len(parsed['items'])} 条，S2 batch 补全中"}))
                entries = discovery.resolve_reference_items(parsed["items"])
                ok_n = sum(1 for e in entries if e.get("resolved"))
                q.put(("discover_results", {"query": f"references({parsed['kind']})",
                                            "entries": entries, "source": "s2-batch"}))
                q.put(("final", {"entries": len(entries), "resolved": ok_n,
                                 "note": f"参考文献下钻：{ok_n}/{len(entries)} 条解析成功"}))
            elif body.title or body.doi or body.arxiv_id:
                label = body.title or body.doi or body.arxiv_id
                q.put(("status", {"stage": "resolve", "note": f"精确解析：{label[:60]}"}))
                r = discovery.resolve_identifier(title=body.title, doi=body.doi,
                                                 arxiv_id=body.arxiv_id)
                if not r.get("ok"):
                    q.put(("error", {"message": f"未命中：{r.get('error', '')}"}))
                    return
                q.put(("discover_results", {"query": label, "entries": r["entries"],
                                            "source": r.get("source", "")}))
                q.put(("final", {"entries": len(r["entries"]), "note": "精确解析完成"}))
            else:
                r = discovery.search_multi(body.query.strip(), body.sources, body.max_results)
                for name, info in (r.get("per_source") or {}).items():
                    q.put(("status", {"stage": "search", "source": name,
                                      "note": f"{name}: {info.get('source')}"
                                      + (f"（{info.get('error')}）" if info.get("error") else "")}))
                q.put(("discover_results", {"query": body.query.strip(),
                                            "entries": r["entries"], "source": "multi"}))
                live = sum(1 for v in (r.get("per_source") or {}).values() if v.get("ok"))
                q.put(("final", {"entries": len(r["entries"]),
                                 "note": f"多源检索完成（{live}/{len(r.get('per_source') or {})} 源在线）"}))
        except Exception as e:  # noqa: BLE001
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            q.put(("done", {}))

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            try:
                kind, data = q.get(timeout=180)
            except queue_mod.Empty:
                yield 'event: error\ndata: {"message":"search timeout"}\n\n'
                return
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if kind == "done":
                return

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/discover/download")
def discover_download(body: DownloadBody):
    """用户勾选后：下载 → 入库 → 等解析（<180s），返回 paper_id 供跳转 chat/reader。"""
    if not body.arxiv_id and not body.pdf_url:
        raise HTTPException(400, "arxiv_id 与 pdf_url 至少一个")
    d = call_tool("explore", "paper_download", None, step_id="discover-download",
                  arxiv_id=body.arxiv_id, pdf_url=body.pdf_url)
    if not d.get("ok"):
        raise HTTPException(502, f"下载失败：{d.get('error')}")
    data = d["data"]
    if data.get("dedup") == "exact_file":
        return {"paper_id": data["paper_id"], "dedup": "exact_file",
                "existing_title": data.get("existing_title"), "job_id": None}
    p = call_tool("explore", "pdf_parse", None, step_id="discover-parse",
                  paper_id=data["paper_id"], wait_s=180)
    return {"paper_id": data["paper_id"], "job_id": data.get("job_id"),
            "parse": p.get("data")}


@router.get("/discover/repro")
def discover_repro(title: str):
    """复现性查分（github_repro 工具的 REST 暴露，discover 卡片懒加载）。"""
    from ..agents.tools import _github_repro

    return _github_repro(title)


@router.get("/sandbox/figure/{name}")
def sandbox_figure(name: str):
    """python_sandbox 产物图（文件名白名单校验，防路径穿越）。"""
    import re

    if not re.fullmatch(r"[0-9a-f]{12}\.png", name):
        raise HTTPException(400, "非法文件名")
    path = DATA_DIR / "figures" / "sandbox" / name
    if not path.exists():
        raise HTTPException(404, "图不存在")
    return FileResponse(path, media_type="image/png")
