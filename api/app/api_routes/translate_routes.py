"""翻译端点（4.7）：block 级 SSE 翻译 + 缓存拉取 + 划词翻译 + 术语表管理。"""
from __future__ import annotations

import queue as queue_mod
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, translate

router = APIRouter(prefix="/api")


class TranslateBody(BaseModel):
    lang: str = "zh"
    page: int | None = None        # DocIR 0 基页码（与 docir 端点一致）
    section: str | None = None     # 章节前缀（section_path startswith）
    block_ids: list[str] | None = None


class SelectionBody(BaseModel):
    text: str
    context: str = ""


class TermBody(BaseModel):
    term: str
    translation: str


@router.post("/papers/{paper_id}/translate")
def translate_paper_sse(paper_id: str, body: TranslateBody):
    """SSE 流式翻译：t_delta（块级增量）→ final（统计）→ done。

    客户端断开 → stop 事件置位，worker 提前停；已完成块已落缓存，重试即续译。
    """
    import json as _json
    from pathlib import Path

    from ..config import DOCIR_DIR

    if not (Path(DOCIR_DIR) / f"{paper_id}.json").exists():
        raise HTTPException(404, "论文未解析或不存在")

    q: queue_mod.Queue = queue_mod.Queue()
    stop = threading.Event()

    def worker() -> None:
        try:
            ids = translate.select_block_ids(
                translate.load_blocks(paper_id), page=body.page,
                section=body.section, block_ids=body.block_ids)

            def progress(done: int, total: int, block_id: str, text: str, ok: bool) -> None:
                q.put(("t_delta", {"block_id": block_id, "text": text, "format_ok": ok,
                                   "done": done, "total": total}))

            stats = translate.translate_blocks(paper_id, ids, lang=body.lang,
                                               progress=progress,
                                               should_stop=stop.is_set)
            q.put(("final", {"done": stats["done"], "total": len(ids),
                             "cached": stats["cached"], "failed": stats["failed"],
                             "format_bad": stats["format_bad"], "stopped": False}))
        except FileNotFoundError as e:
            q.put(("error", {"message": str(e)}))
        except Exception as e:  # noqa: BLE001 —— 翻译失败必须可观测而非 500
            q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            q.put(("done", {}))

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        try:
            while True:
                try:
                    kind, data = q.get(timeout=1800)
                except queue_mod.Empty:
                    stop.set()
                    yield 'event: error\ndata: {"message":"translate timeout"}\n\n'
                    return
                yield f"event: {kind}\ndata: {_json.dumps(data, ensure_ascii=False)}\n\n"
                if kind == "done":
                    return
        finally:
            stop.set()   # 客户端断开 → 续译靠缓存

    from fastapi.responses import StreamingResponse
    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/papers/{paper_id}/translation")
def get_translation(paper_id: str, lang: str = "zh"):
    return {"lang": lang, "blocks": translate.cached_translations(paper_id, lang)}


@router.post("/translate/selection")
def translate_selection(body: SelectionBody):
    if not body.text.strip():
        raise HTTPException(400, "text 为空")
    out = translate.translate_selection(body.text.strip(), body.context.strip())
    if not out.get("ok") and not out.get("translation"):
        return out | {"translation": "（翻译不可用：LLM 未配置或调用失败）", "term_hit": None}
    return out


@router.get("/terminology")
def get_terminology():
    return {"terms": db.list_terminology()}


@router.post("/terminology")
def add_terminology(body: TermBody):
    if not body.term.strip() or not body.translation.strip():
        raise HTTPException(400, "term/translation 均不能为空")
    db.upsert_terminology(body.term, body.translation, source="manual")
    return {"ok": True, "terms": db.list_terminology()}


@router.delete("/terminology/{term}")
def remove_terminology(term: str):
    db.delete_terminology(term)
    return {"ok": True, "terms": db.list_terminology()}
