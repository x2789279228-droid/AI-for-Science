"""全量重建 chunk 索引（v2.3.0 分块改造配套）：从已存 DocIR 重跑 chunk_docir
→ 删旧 chunks（含 FTS）→ index_chunks 重嵌入。

背景：分块改进只对新上传文档生效——表格块带标题/章节/上文、枚举/SOP 块逐项拆细
（"底放冰块隔冰打奶盖"类关键句原先被并进 ~900 字大块）。已入库文档需本脚本
重建才能受益。嵌入走 .env 配置的后端（方舟套餐内），每篇 ~数百块、批 8 限速。

幂等：按 paper_id 删旧插新（index_chunks 自带 DELETE）；DocIR 缺失的论文跳过
并如实列出（其旧 chunks 保持不动）。
用法：cd api && uv run python scripts/reindex_chunks.py [--dry] [--paper p_xxx]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db
from app.config import DOCIR_DIR, settings
from app.jobs import _index_view
from app.rag.chunking import chunk_docir
from app.rag.indexing import index_chunks


def main() -> int:
    dry = "--dry" in sys.argv
    only = ""
    if "--paper" in sys.argv:
        only = sys.argv[sys.argv.index("--paper") + 1]
    db.init_db()
    rows = db.query("SELECT id, title FROM papers WHERE status='active' ORDER BY created_at")
    if only:
        rows = [r for r in rows if r["id"] == only]
    total_old = total_new = rebuilt = skipped = 0
    for r in rows:
        pid = r["id"]
        path = DOCIR_DIR / f"{pid}.json"
        title = r["title"] or ""   # SELECT 恒取 title 列
        if not path.is_file():
            print(f"SKIP {pid} · {title[:30]} · 无 DocIR（{path.name}），旧索引保持")
            skipped += 1
            continue
        docir = json.loads(path.read_text(encoding="utf-8"))
        # 与入库管线同口径（jobs.py）：长文档只对前 N 页做全文 chunk/索引
        if (docir.get("meta") or {}).get("long_doc"):
            docir = _index_view(docir, settings.long_doc_index_pages)
        chunks = chunk_docir(docir)
        old_n = db.query("SELECT COUNT(*) c FROM chunks WHERE paper_id=?", (pid,))[0]["c"]
        print(f"{'DRY ' if dry else ''}{pid} · {title[:30]} · "
              f"chunks {old_n} → {len(chunks)}")
        total_old += old_n
        total_new += len(chunks)
        if dry:
            continue
        n = index_chunks(pid, chunks)
        rebuilt += 1
        print(f"     done: {n} chunks indexed (vec backend on)")
    print(f"\n== {'DRY ' if dry else ''}汇总：{rebuilt} 篇重建 · {skipped} 篇跳过 · "
          f"chunks {total_old} → {total_new} ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
