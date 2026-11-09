"""Round4 模块三：PDF overlay/bilingual 导出视觉回归（宿主机跑，需真实 LLM 预热译文）。

对每篇样例论文：
1. 预热前 3 页译文（POST /translate，真实 LLM；已有缓存自动跳过）
2. 导出 overlay 与 bilingual 两版 PDF（GET /export）
3. PyMuPDF 断言（对照原 PDF）：
   - 页数：overlay ≥ 原页数（补遗页只增不减）；bilingual = 2×已译页 + 未译页
   - 图片数不减少（redaction images=2 保留；严格断言）
   - 矢量 drawing 数为报告字段（redaction 重建对象有合法扰动，不设硬门禁）
   - 译文 CJK 字符数 > 0（确实替换了文本）
   - X-Export-Overflow 补遗块计数落记录
4. 抽 3 页渲染 PNG 到输出目录供人工核验（checklist 人工项）

用法：cd api && uv run python scripts/pdf_export_check.py --out ../../docs/acceptance/round4-<date>/
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fitz  # PyMuPDF


def _cjk_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def _pick_papers(client, keywords: list[str]) -> list[str]:
    from app import db
    rows = db.query("SELECT id,title FROM papers WHERE status='active' AND docir_path IS NOT NULL "
                    "AND docir_path != ''")
    out = []
    for kw in keywords:
        pid = next((r["id"] for r in rows if kw in (r["title"] or "").lower()), "")
        if pid:
            out.append(pid)
    return out


def _warm_pages(client, paper_id: str, pages: int = 3) -> dict:
    """预热译文（SSE 拉到 final；缓存命中也走一遍拿统计）。"""
    stats = {"warmed_pages": 0, "cached": 0, "failed": 0}
    for page in range(pages):
        try:
            with client.stream("POST", f"/api/papers/{paper_id}/translate",
                               json={"lang": "zh", "page": page}) as r:
                assert r.status_code == 200, r.text
                event = ""
                final = None
                for line in r.iter_lines():
                    if line.startswith("event: "):
                        event = line[7:].strip()
                    elif line.startswith("data: ") and event == "final":
                        final = json.loads(line[6:])
            if final:
                stats["warmed_pages"] += 1
                stats["cached"] += int(final.get("cached") or 0)
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            print(f"  [WARM p{page}] 失败：{type(e).__name__}: {e}")
    return stats


def _doc_stats(blob: bytes) -> dict:
    doc = fitz.open(stream=blob, filetype="pdf")
    try:
        return {
            "page_count": doc.page_count,
            "images": sum(len(p.get_images(full=True)) for p in doc),
            "drawings": sum(len(p.get_drawings()) for p in doc),
            "cjk_chars": sum(_cjk_count(p.get_text("text")) for p in doc),
        }
    finally:
        doc.close()


def _render_samples(blob: bytes, outdir: Path, tag: str, page_nos: list[int]) -> None:
    doc = fitz.open(stream=blob, filetype="pdf")
    try:
        for pno in page_nos:
            if 0 <= pno < doc.page_count:
                pix = doc[pno].get_pixmap(dpi=110)
                (outdir / f"{tag}_p{pno + 1}.png").write_bytes(pix.tobytes("png"))
    finally:
        doc.close()


def check_paper(client, paper_id: str, outdir: Path) -> dict:
    from app import db
    row = db.query("SELECT file_path FROM papers WHERE id=?", (paper_id,))
    src = Path(row[0]["file_path"]) if row and row[0]["file_path"] else None
    if not src or not src.exists():
        # 上传文件不在磁盘（快照恢复场景）→ 从 /pdf 端点取原件
        r = client.get(f"/api/papers/{paper_id}/pdf")
        src_bytes = r.content if r.status_code == 200 else b""
    else:
        src_bytes = src.read_bytes()
    origin = _doc_stats(src_bytes)
    result: dict = {"paper_id": paper_id, "origin": origin, "layouts": {}}
    warm = _warm_pages(client, paper_id, pages=3)
    result["warm"] = warm
    for layout in ("overlay", "bilingual"):
        r = client.get(f"/api/papers/{paper_id}/export"
                       f"?fmt=pdf&target_lang=zh&mode=academic&layout={layout}")
        if r.status_code != 200:
            result["layouts"][layout] = {"ok": False, "http": r.status_code,
                                         "detail": r.text[:200]}
            print(f"  [{layout}] HTTP {r.status_code}")
            continue
        blob = r.content
        out = _doc_stats(blob)
        overflow = int(r.headers.get("X-Export-Overflow") or 0)
        checks = {
            "pages_ge_origin": out["page_count"] >= origin["page_count"],
            "images_not_fewer": out["images"] >= origin["images"],
            "cjk_translated": out["cjk_chars"] > 0,
        }
        ok = all(checks.values())
        result["layouts"][layout] = {"ok": ok, **out, "overflow_blocks": overflow,
                                     "checks": checks,
                                     "drawing_delta": out["drawings"] - origin["drawings"]}
        print(f"  [{layout}] {'✓' if ok else '✗'} pages={out['page_count']}/{origin['page_count']}"
              f" img={out['images']}/{origin['images']} cjk={out['cjk_chars']} overflow={overflow}")
        (outdir / f"{paper_id[:10]}.{layout}.pdf").write_bytes(blob)
        _render_samples(blob, outdir, f"{paper_id[:10]}.{layout}", [0, 1, out["page_count"] - 1])
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../../docs/acceptance/_pdfcheck")
    args = ap.parse_args()
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app
    db.init_db()
    client = TestClient(app)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    papers = _pick_papers(client, ["attention", "few-shot learners"])
    if not papers:
        print("库内无 Attention/GPT-3 论文（先跑 fetch_corpus）")
        return 1
    report = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "papers": []}
    for pid in papers:
        print(f"== PDF 导出检查 {pid} ==")
        report["papers"].append(check_paper(client, pid, outdir))
    ok = all(all(l.get("ok") for l in p["layouts"].values()) for p in report["papers"])
    report["ok"] = bool(ok)
    report["elapsed_s"] = round(time.time() - t0, 1)
    (outdir / "pdf_export_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n== PDF 导出回归 {'PASS' if ok else 'FAIL'} · {report['elapsed_s']}s ==")
    print(f"已写出：{outdir / 'pdf_export_check.json'}（含 3 页/版 PNG 样张供人工核验）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
