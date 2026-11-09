"""学术写作与数据可视化 Copilot 端点（模块六）。

- POST /api/copilot/writing：无幻觉论文框架 SSE（大纲 → 分节 → References 白名单后验）
- POST /api/copilot/datasets：CSV 上传（护栏：≤5MB/≤10万行/编码嗅探/公式注入清洗）
- GET  /api/copilot/datasets、GET /{id}：数据集列表与预览
- POST /api/copilot/chart：四类图 spec + 确定性学术图注（数字可回溯数据集）
- POST /api/copilot/diagram：DiagramSpec → Mermaid/DOT/TikZ 源码 + matplotlib PNG
- POST /api/copilot/translate：中→英学术翻译（复用模块三 target_lang=en）
"""
from __future__ import annotations

import json
import queue as queue_mod
import threading
import time

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import db, translate
from ..agents import copilot as copilot_agent
from ..agents import csv_viz, diagram

router = APIRouter(prefix="/api/copilot")


class WritingBody(BaseModel):
    idea: str
    paper_ids: list[str]
    sections: list[str] | None = None


@router.post("/writing")
def writing(body: WritingBody):
    """论文框架 SSE：status（分节进度）→ final {outline, sections, references, guardrails}。"""
    if not body.idea.strip():
        raise HTTPException(400, "idea 为空")
    if not body.paper_ids:
        raise HTTPException(400, "paper_ids 为空（框架必须基于库内证据）")
    q: queue_mod.Queue = queue_mod.Queue()

    def worker() -> None:
        try:
            def prog(name: str) -> None:
                q.put(("status", {"stage": "section", "note": f"起草：{name}"}))

            out = copilot_agent.writing_framework(body.idea.strip(), body.paper_ids,
                                                  sections=body.sections, progress=prog)
            if out.get("error"):
                q.put(("error", {"message": out["error"]}))
                return
            with db.tx() as c:
                c.execute("INSERT INTO writing_drafts(id,idea,paper_ids,outline,sections,"
                          "references_json,guardrails,created_at) VALUES(?,?,?,?,?,?,?,?)",
                          (db.new_id("wd_"), body.idea[:300],
                           json.dumps(body.paper_ids), json.dumps(out["outline"]),
                           json.dumps(out["sections"]), json.dumps(out["references"]),
                           json.dumps(out.get("guardrails", [])), time.time()))
            q.put(("final", out))
        except Exception as e:  # noqa: BLE001
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            q.put(("done", {}))

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            try:
                kind, data = q.get(timeout=600)
            except queue_mod.Empty:
                yield 'event: error\ndata: {"message":"writing timeout"}\n\n'
                return
            yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if kind == "done":
                return

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/datasets")
async def upload_dataset(file: UploadFile = File(...)):  # noqa: B008 —— FastAPI 惯用法
    """CSV 上传：大小/行数护栏 + 编码/分隔符嗅探 + 列类型推断 + 公式注入清洗。"""
    raw = await file.read()
    try:
        out = csv_viz.store_dataset(raw, file.filename or "dataset.csv")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if out["truncated"]:
        out["note"] = f"行数超 10 万，已截断保留前 {out['row_count']} 行"
    return out


@router.get("/datasets")
def list_datasets():
    rows = db.query("SELECT id,name,columns,row_count,created_at FROM csv_datasets "
                    "ORDER BY created_at DESC LIMIT 50")
    return {"datasets": [dict(r) | {"columns": json.loads(r["columns"])} for r in rows]}


@router.get("/datasets/{ds_id}")
def get_dataset(ds_id: str):
    ds = csv_viz.load_dataset(ds_id)
    if ds is None:
        raise HTTPException(404, "数据集不存在")
    ds["rows"] = ds["rows"][:50]     # 预览仅前 50 行
    return ds


class ChartBody(BaseModel):
    dataset_id: str
    chart_type: str                 # table | line | bar | radar
    x: str | None = None
    y: list[str] | None = None
    title: str = ""
    with_caption: bool = True


@router.post("/chart")
def chart(body: ChartBody):
    """四类图 + 学术图注。caption 数字全部来自数据集统计（可回溯）。"""
    ds = csv_viz.load_dataset(body.dataset_id)
    if ds is None:
        raise HTTPException(404, "数据集不存在")
    try:
        out = csv_viz.build_chart(ds, body.chart_type, body.x, body.y, body.title)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    spec = out["spec"]
    caption = ""
    if body.with_caption and spec.get("type") in ("line", "bar", "radar"):
        x_col = spec["x"]["name"]
        y_col = spec["series"][0]["name"] if spec.get("series") else ""
        if y_col:
            caption = csv_viz.make_caption(ds, spec, x_col, y_col)
    art_id = db.new_id("va_")
    with db.tx() as c:
        c.execute("INSERT INTO viz_artifacts(id,dataset_id,spec,caption,created_at)"
                  " VALUES(?,?,?,?,?)",
                  (art_id, body.dataset_id, json.dumps(spec, ensure_ascii=False),
                   caption, time.time()))
    return {"id": art_id, "spec": spec, "caption": caption}


class DiagramBody(BaseModel):
    description: str
    fmt: str = "all"                # all | mermaid | dot | tikz | matplotlib


@router.post("/diagram")
def diagram_route(body: DiagramBody):
    """拓扑图脚本：LLM 只产 DiagramSpec，四格式确定性渲染 + 语法校验。"""
    if not body.description.strip():
        raise HTTPException(400, "description 为空")
    try:
        return diagram.generate_diagram(body.description.strip(), body.fmt)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, f"spec 无效：{e}") from e
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}") from e


class Zh2EnBody(BaseModel):
    text: str = ""
    paper_id: str = ""              # 库内论文整篇/摘要（二选一）
    scope: str = "selection"        # selection | abstract | full


@router.post("/translate")
def zh2en(body: Zh2EnBody):
    """中→英学术翻译（模块六学术翻译 Tab，复用模块三双向管线）。"""
    if body.paper_id:
        ids = translate.select_block_ids(translate.load_blocks(body.paper_id),
                                         include_extras=False)
        if body.scope == "abstract":
            ids = ids[:6]
        if not ids:
            raise HTTPException(404, "论文无可译块")
        out_blocks: dict[str, str] = {}

        def prog(done, total, bid, text, ok):
            out_blocks[bid] = text

        stats = translate.translate_blocks(body.paper_id, ids, lang="en",
                                           mode="academic", progress=prog)
        return {"stats": stats, "blocks": out_blocks}
    if not body.text.strip():
        raise HTTPException(400, "text 与 paper_id 至少一个")
    if translate.detect_lang(body.text) != "zh":
        raise HTTPException(400, "检测到输入不是中文（本端点专做中→英学术翻译）")
    out = translate.translate_selection(body.text.strip(), target_lang="en", mode="academic")
    return out
