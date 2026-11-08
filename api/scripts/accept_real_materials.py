"""真素材验收（官方用例 3.1/3.2）：VLM 扫描件通道 + UDL 541 页长文档。

用法：
  uv run python scripts/accept_real_materials.py           # 全部
  uv run python scripts/accept_real_materials.py vlm       # 只跑 3.2a-2 VLM 通道
  uv run python scripts/accept_real_materials.py udl       # 只跑 3.2b UDL MinerU 重解析
前置：downloads/1706.03762/{1706.03762v1_img.pdf}、downloads/udl/UnderstandingDeepLearning_02_09_26_C.pdf
输出：验收标记行（[ACCEPT-*]），失败项带原因。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

V1_IMG = Path("D:/传智杯/downloads/1706.03762/1706.03762v1_img.pdf")
UDL = Path("D:/传智杯/downloads/udl/UnderstandingDeepLearning_02_09_26_C.pdf")


def accept_3_2a_vlm() -> None:
    """3.2a-2：MinerU 关闭 → 扫描件走 VLM 通道（doubao-seed-2.1-lite 整页识读）。"""
    from app.config import settings
    from app.parsing.router import parse_document

    old = settings.mineru_enabled
    settings.mineru_enabled = "off"   # 强制 VLM 通道
    try:
        print("=== 3.2a-2 VLM channel on v1_img ===", flush=True)
        t0 = time.time()
        d = parse_document(V1_IMG, progress=lambda f, m: None)
        m = d["meta"]
        q = m["quality"]
        print(f"[ACCEPT-32A-VLM] engine={m.get('parse_engine')} elapsed={time.time()-t0:.0f}s "
              f"blocks={len(d['blocks'])} toc={len(d['toc'])} formulas={q['formula_count']} "
              f"tables={q['table_count']} coverage={q['text_coverage']} "
              f"vlm_failed_pages={m.get('vlm_failed_pages')} "
              f"fallback={m.get('fallback_reason')}", flush=True)
        heads = [b["text"][:30] for b in d["blocks"] if b["type"] == "heading"][:4]
        print("  headings:", heads, flush=True)
        eqs = [b["text"][:50] for b in d["blocks"] if b["type"] == "formula"][:2]
        print("  formulas:", eqs, flush=True)
    finally:
        settings.mineru_enabled = old


def accept_3_2b_longdoc_mineru() -> None:
    """3.2b：UDL 541 页 MinerU 整本重解析（覆盖 PyMuPDF 兜底版本，超时按页数扩展）。"""
    from app import db
    from app.config import DOCIR_DIR
    from app.jobs import submit_parse

    print("=== 3.2b UDL MinerU whole-book re-parse ===", flush=True)
    db.init_db()
    rows = db.query("SELECT id FROM papers WHERE title LIKE '%Understanding Deep Learning%'")
    if not rows:
        print("[ACCEPT-32B] 未找到 UDL 论文行（先经 upload_pdf 入库）", flush=True)
        return
    pid = rows[0]["id"]
    import hashlib
    h = hashlib.sha256(UDL.read_bytes()).hexdigest()
    with db.tx() as c:
        c.execute("UPDATE papers SET file_hash=? WHERE id=?", (h, pid))
        c.execute("DELETE FROM jobs WHERE paper_id=? AND kind='dedup_alert'", (pid,))
        c.execute("UPDATE papers SET supersedes='' WHERE id=?", (pid,))
    job_id = submit_parse(pid)
    print(f"re-parse pid={pid} job={job_id}", flush=True)
    t0 = time.time()
    last = ""
    while time.time() - t0 < 7500:   # 上限 125 分钟（541 页 × ~7s/页 + 余量）
        job = db.get_job(job_id)
        if job["status"] in ("done", "error"):
            break
        if job["message"] != last:
            last = job["message"]
            print(f"  [{time.time()-t0:.0f}s] {job['progress']:.2f} {job['message']}", flush=True)
        time.sleep(10)
    job = db.get_job(job_id)
    print(f"[ACCEPT-32B] job={job['status']} elapsed={time.time()-t0:.0f}s msg={job['message']}", flush=True)
    if job["status"] != "done":
        return
    d = json.loads((DOCIR_DIR / f"{pid}.json").read_text(encoding="utf-8"))
    m = d["meta"]
    n_indexed = db.query("SELECT COUNT(*) c FROM chunks WHERE paper_id=?", (pid,))[0]["c"]
    print(f"[ACCEPT-32B-DETAIL] engine={m.get('parse_engine')} pages={m['page_count']} "
          f"blocks={len(d['blocks'])} toc={len(d['toc'])} tables={len(d['tables'])} "
          f"figures={len(d['figures'])} chunks_indexed={n_indexed} quality={json.dumps(m.get('quality'))}",
          flush=True)
    formulas = [b["text"][:60] for b in d["blocks"] if b["type"] == "formula"][:3]
    print("  formula samples:", formulas, flush=True)


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else "all"
    if only in ("all", "vlm"):
        accept_3_2a_vlm()
    if only in ("all", "udl"):
        accept_3_2b_longdoc_mineru()
    print("ALL DONE", flush=True)
