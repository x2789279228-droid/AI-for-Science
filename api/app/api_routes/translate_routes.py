"""翻译端点（4.7）：block 级 SSE 翻译 + 缓存拉取 + 划词翻译 + 术语表管理。"""
from __future__ import annotations

import queue as queue_mod
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, translate

router = APIRouter(prefix="/api")


class TranslateBody(BaseModel):
    lang: str = "zh"               # 兼容旧客户端（等价 target_lang）
    target_lang: str | None = None  # zh | en（zh→en = 中→英学术翻译，模块六同源）
    source_lang: str = "auto"       # auto | zh | en（auto 按 CJK 占比检测）
    mode: str = "academic"          # academic | literal
    page: int | None = None        # DocIR 0 基页码（与 docir 端点一致）
    section: str | None = None     # 章节前缀（section_path startswith）
    block_ids: list[str] | None = None
    include_extras: bool = False   # P1：纳入表格 markdown 与图注（伪块 id t#/f#）


class SelectionBody(BaseModel):
    text: str
    context: str = ""
    target_lang: str = "zh"
    mode: str = "academic"


def _validated_dir(paper_id: str, body: TranslateBody) -> str:
    """方向校验：target ∈ {zh,en}；source 显式或检测后不得与 target 同语。
    auto 检测取选中块的前几块拼样本文本（CJK 占比判 zh/en）。"""
    target = (body.target_lang or body.lang or "zh").lower()
    if target not in ("zh", "en"):
        raise HTTPException(400, "target_lang 只支持 zh | en")
    if body.mode not in ("academic", "literal"):
        raise HTTPException(400, "mode 只支持 academic | literal")
    src = (body.source_lang or "auto").lower()
    if src not in ("auto", "zh", "en"):
        raise HTTPException(400, "source_lang 只支持 auto | zh | en")
    if src == "auto":
        docir = translate.load_blocks(paper_id)
        ids = translate.select_block_ids(docir, page=body.page, section=body.section,
                                         block_ids=body.block_ids)
        by_id = {b["id"]: b.get("text") or "" for b in docir.get("blocks", [])}
        sample = "".join(by_id.get(bid, "") for bid in ids[:3])
        src = translate.detect_lang(sample)
    if src == target:
        raise HTTPException(400, f"检测到源语言({src})与目标语言({target})相同，无需翻译")
    return target


class TermBody(BaseModel):
    term: str
    translation: str


@router.post("/papers/{paper_id}/translate")
def translate_paper_sse(paper_id: str, body: TranslateBody):
    """SSE 流式翻译：t_delta（块级增量）→ final（统计）→ done。

    客户端断开 → stop 事件置位，worker 提前停；已完成块已落缓存，重试即续译。
    """
    import json as _json

    from ..rag.version_diff import load_docir_for

    if load_docir_for(paper_id) is None:   # 与 docir 端点同源判据（DB 路径 + 目录回退）
        raise HTTPException(404, "论文未解析或不存在")
    target = _validated_dir(paper_id, body)

    q: queue_mod.Queue = queue_mod.Queue()
    stop = threading.Event()

    def worker() -> None:
        try:
            ids = translate.select_block_ids(
                translate.load_blocks(paper_id), page=body.page,
                section=body.section, block_ids=body.block_ids,
                include_extras=body.include_extras)

            def progress(done: int, total: int, block_id: str, text: str, ok: bool) -> None:
                q.put(("t_delta", {"block_id": block_id, "text": text, "format_ok": ok,
                                   "done": done, "total": total}))

            stats = translate.translate_blocks(paper_id, ids, lang=target, mode=body.mode,
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
def get_translation(paper_id: str, lang: str = "zh", mode: str = "academic"):
    return {"lang": lang, "mode": mode,
            "blocks": translate.cached_translations(paper_id, lang, mode)}


@router.post("/translate/selection")
def translate_selection(body: SelectionBody):
    if not body.text.strip():
        raise HTTPException(400, "text 为空")
    if body.target_lang not in ("zh", "en") or body.mode not in ("academic", "literal"):
        raise HTTPException(400, "target_lang/mode 参数不合法")
    out = translate.translate_selection(body.text.strip(), body.context.strip(),
                                        target_lang=body.target_lang, mode=body.mode)
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


@router.get("/terminology/export")
def export_terminology():
    """术语表导出（CSV 文本，UTF-8；term,translation,source 三列）。"""
    import csv as _csv
    import io as _io

    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["term", "translation", "source"])
    for t in db.list_terminology():
        w.writerow([t.get("term", ""), t.get("translation", ""), t.get("source", "")])
    from fastapi.responses import Response
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=terminology.csv"})


@router.post("/terminology/import")
def import_terminology(body: dict):
    """术语表批量导入：body.csv（CSV 文本，term,translation 列；兼容制表符/分号分隔）。
    跳过空行/表头/非法行并如实计数，非法输入不静默。"""
    import csv as _csv
    import io as _io

    csv_text = (body.get("csv") or "").strip()
    if not csv_text:
        raise HTTPException(400, "csv 为空")
    delim = "\t" if "\t" in csv_text.splitlines()[0] else (";" if csv_text.splitlines()[0].count(";") > csv_text.splitlines()[0].count(",") else ",")
    imported = skipped = 0
    for i, row in enumerate(_csv.reader(_io.StringIO(csv_text), delimiter=delim)):
        if not row or not (row[0] or "").strip():
            skipped += 1
            continue
        term, trans = row[0].strip(), (row[1] or "").strip() if len(row) > 1 else ""
        if not trans or term.lower() in ("term", "术语"):
            skipped += 1
            continue
        db.upsert_terminology(term, trans, source="import")
        imported += 1
    return {"ok": True, "imported": imported, "skipped": skipped,
            "terms": db.list_terminology()}
