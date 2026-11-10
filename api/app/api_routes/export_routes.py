"""导出端点（模块三 P0-2）：译文 PDF（overlay/bilingual）+ MD/HTML。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..export import pdf_export

router = APIRouter(prefix="/api")


@router.get("/papers/{paper_id}/export")
def export_paper(paper_id: str, fmt: str = "pdf", target_lang: str = "zh",
                 mode: str = "academic", layout: str = "overlay", view: str = "mono"):
    """导出译文文档。fmt=pdf（layout=overlay|bilingual）| md | html（view=mono|bilingual）。

    PDF 同步生成（20 页秒级）+ 缓存复用；未翻译 → 400 结构化提示，不产出空壳文件。
    """
    if fmt not in ("pdf", "md", "html"):
        raise HTTPException(400, "fmt 只支持 pdf | md | html")
    if target_lang not in ("zh", "en") or mode not in ("academic", "literal"):
        raise HTTPException(400, "target_lang/mode 参数不合法")
    try:
        if fmt == "pdf":
            if layout not in ("overlay", "bilingual"):
                raise HTTPException(400, "layout 只支持 overlay | bilingual")
            out = pdf_export.export_pdf(paper_id, target_lang=target_lang, mode=mode,
                                        layout=layout)
            return FileResponse(out["path"], media_type="application/pdf",
                                filename=f"{paper_id}.{target_lang}.{layout}.pdf",
                                headers={"X-Export-Pages": str(out["pages"]),
                                         "X-Export-Replaced": str(out["replaced"]),
                                         "X-Export-Overflow": str(len(out["overflow_blocks"])),
                                         # 覆盖率如实上报（bilingual 版仅含已缓存译文的
                                         # 原文页的译文页——Round5 visual-judge 发现的口径缺口）
                                         "X-Export-Coverage": f"{out['translated_pages']}/{out['source_pages']}"})
        out = pdf_export.export_doc(paper_id, target_lang=target_lang, mode=mode, fmt=fmt,
                                    bilingual_view=(view == "bilingual"))
        media = "text/markdown" if fmt == "md" else "text/html"
        return FileResponse(out["path"], media_type=media,
                            filename=out["path"].split("\\")[-1].split("/")[-1])
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except LookupError as e:
        raise HTTPException(400, str(e)) from e
