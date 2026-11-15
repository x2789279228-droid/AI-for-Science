"""统计·多 seed CI（Round6）：同一生成题集跑 3 轮（LLM 端非确定性即为波动源），
报 supported_ratio / anchor_precision / 幻觉率的均值、标准差与 95%CI 半宽
（t 分布，df=2 → t=4.303；n=3 如实标注小样本口径）。

评分表「统计·seed 间 95%CI 半宽」行的证据源。
用法：cd api && uv run python scripts/eval_seeds.py --rounds 3
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_DATA = Path(__file__).parent.parent / "tests" / f"_seed_{int(time.time())}"
os.environ.setdefault("SCHOLAR_DATA_DIR", str(_DATA))
os.environ.setdefault("MINERU_ENABLED", "off")
os.environ.setdefault("RATE_LIMIT_PER_MIN", "0")
# LLM 凭据尊重宿主机 .env（生成指标需要真实 LLM）

from fastapi.testclient import TestClient

from app import db
from app.main import app

QS = [
    "How many GPUs were the base models trained on?",
    "What BLEU score did the big model achieve on EN-DE?",
    "Which optimizer was used?",
    "What is the model dimension d_model?",
    "How many encoder layers are there?",
]


def _pick_paper() -> str:
    rows = db.query("SELECT id FROM papers WHERE docir_path IS NOT NULL AND docir_path != '' "
                    "AND COALESCE(doc_type,'paper')='paper' ORDER BY created_at DESC")
    for r in rows:
        t = (db.query("SELECT title FROM papers WHERE id=?", (r["id"],))[0]["title"] or "").lower()
        if "attention is all you need" in t:
            return r["id"]
    return rows[0]["id"] if rows else ""


def _ensure_paper(client: TestClient) -> str:
    pid = _pick_paper()
    if pid:
        return pid
    import io
    pdf = Path(__file__).resolve().parents[3] / "downloads" / "1706.03762" / "1706.03762v7.pdf"
    r = client.post("/api/papers", files={"file": ("v7.pdf", io.BytesIO(pdf.read_bytes()),
                                                  "application/pdf")}, data={"doc_type": "paper"})
    job = r.json().get("job_id")
    while job:
        j = client.get(f"/api/jobs/{job}").json()
        if j.get("status") in ("done", "error", "cancelled"):
            break
        time.sleep(0.3)
    return _pick_paper()


def one_round(client: TestClient, pid: str) -> dict:
    supported = claims_n = anchors_ok = anchors_n = 0
    for q in QS:
        conv = f"seed_{time.time_ns()}"
        final = None
        event = ""
        buf = b""
        sep = b"\n\n"
        with client.stream("POST", "/api/chat", json={
                "question": q, "paper_ids": [pid], "conversation_id": conv,
                "agent": "reader", "doc_mode": "paper"}) as r:
            for chunk in r.iter_bytes():
                buf += chunk
                while sep in buf:
                    frame, buf = buf.split(sep, 1)
                    for line in frame.decode("utf-8", "replace").splitlines():
                        if line.startswith("event: "):
                            event = line[7:].strip()
                        elif line.startswith("data: ") and event == "final":
                            final = json.loads(line[6:])
                    if event == "done":
                        break
                if event == "done":
                    break
        final = final or {}
        cl = final.get("claims") or []
        for c in cl:
            claims_n += 1
            if c.get("status") == "supported":
                supported += 1
            anchors_n += 1
            if c.get("status") in ("supported",) and c.get("support"):
                anchors_ok += 1
    return {
        "supported_ratio": round(supported / claims_n, 3) if claims_n else None,
        "hallucination_rate": round(1 - supported / claims_n, 3) if claims_n else None,
        "anchor_precision": round(anchors_ok / anchors_n, 3) if anchors_n else None,
        "claims": claims_n,
    }


def ci_half(vals: list[float]) -> float | None:
    """95%CI 半宽（t 分布 df=n-1；n=3 → t=4.303，小样本口径如实标注）。"""
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    t_crit = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776}[len(vals)]
    return round(t_crit * statistics.stdev(vals) / math.sqrt(len(vals)), 3)


def main() -> int:
    rounds = 3
    db.init_db()
    client = TestClient(app)
    pid = _ensure_paper(client)
    assert pid, "无可用论文"
    print(f"paper={pid} rounds={rounds} qs={len(QS)}")
    runs = []
    for i in range(rounds):
        m = one_round(client, pid)
        runs.append(m)
        print(f"  seed#{i + 1}: {m}", flush=True)
    report: dict = {"paper_id": pid, "rounds": rounds, "runs": runs, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    for k in ("supported_ratio", "hallucination_rate", "anchor_precision"):
        vals = [r.get(k) for r in runs]
        vals = [v for v in vals if v is not None]
        if vals:
            mean = round(statistics.mean(vals), 3)
            half = ci_half(vals)
            sd = round(statistics.stdev(vals), 3) if len(vals) > 1 else 0.0
            report[k] = {"mean": mean, "stdev": sd, "ci95_half": half,
                         "values": vals, "ci_note": f"t 分布 df={len(vals) - 1}（小样本口径）"}
            print(f"  {k}: mean={mean} ± {half} (95%CI, stdev {sd})")
    out = Path(__file__).parent / "seeds_ci.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
