"""dense 向量回填（Round6-P1）：修复「入库时 embedding 失败 → 整篇 vec IS NULL →
dense 通道静默缺席」的历史损伤。

实测（2026-11-15）：21 篇在册论文中 11 篇 vec 全缺（~2000 chunk）；BM25 对纯中文
查询零召回（latin token 驱动），dense 是中文维度的唯一检索通道——缺向量的论文在
中文提问下必然检索劣化（compare 金标 Q1 首页版权块冒充命中的根因）。

幂等：只补 WHERE vec IS NULL 的行；批 16 走 indexing._embed_texts（含 429 退避）。
用法：cd api && uv run python scripts/backfill_embeddings.py [--dry]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.rag.indexing import _embed_texts, _pack  # noqa: E402


def main() -> int:
    dry = "--dry" in sys.argv
    db.init_db()
    rows = db.query(
        "SELECT id,paper_id,seq,text FROM chunks WHERE vec IS NULL "
        "ORDER BY paper_id,seq")
    if not rows:
        print("全库无缺向量 chunk，无需回填")
        return 0
    papers = sorted({r["paper_id"] for r in rows})
    print(f"待回填 chunk={len(rows)} 覆盖论文={len(papers)}")
    if dry:
        return 0

    done = fail = 0
    B = 16
    for b0 in range(0, len(rows), B):
        batch = rows[b0:b0 + B]
        vecs = _embed_texts([r["text"] or "" for r in batch])
        if not vecs or len(vecs) != len(batch):
            fail += len(batch)
            print(f"  batch@{b0}: embed 失败（{len(batch)} 条跳过，稍后可重跑）", flush=True)
            continue
        with db.tx() as c:
            for r, v in zip(batch, vecs):
                c.execute("UPDATE chunks SET vec=? WHERE id=?", (_pack(v), r["id"]))
        done += len(batch)
        if (b0 // B) % 5 == 0:
            print(f"  进度 {done}/{len(rows)}", flush=True)
        time.sleep(0.2)  # 批间隔防限流
    after = db.query("SELECT COUNT(*) c FROM chunks WHERE vec IS NULL")[0]["c"]
    total = db.query("SELECT COUNT(*) c FROM chunks")[0]["c"]
    report = {"done": done, "failed": fail, "still_null": after, "total_chunks": total,
              "coverage": round((total - after) / total, 3) if total else None,
              "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    (Path(__file__).parent / "backfill_embeddings.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"回填完成 done={done} failed={fail} 剩余缺向量={after}/{total} "
          f"覆盖率={report['coverage']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
