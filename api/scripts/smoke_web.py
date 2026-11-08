"""真 HTTP 冒烟（区别于 pytest 内 TestClient 级 e2e）：部署后一键 `make smoke`。

前置：栈已起（make api / docker compose up）。覆盖：
health → outline 载荷 → 上传 fixture PDF → 轮询解析 → chat SSE（final+claims/anchors）
→ docir 按页 → translate 缓存端点 → terminology。
退出码非 0 表示冒烟失败。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

API = os.environ.get("API_BASE", "http://127.0.0.1:8100")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {('· ' + detail) if detail else ''}")


def sse_events(resp_lines) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event = ""
    for raw in resp_lines:
        line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            try:
                events.append((event, json.loads(line[6:])))
            except json.JSONDecodeError:
                pass
    return events


def main() -> int:
    import httpx

    c = httpx.Client(timeout=120)

    # ① health
    r = c.get(f"{API}/api/health")
    check("health", r.status_code == 200 and r.json().get("status") == "ok",
          f"papers={r.json().get('papers') if r.status_code == 200 else '?'}")

    # ② 上传 fixture PDF → 轮询解析
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.fixtures_pdf import make_paper_pdf

    tmp = Path(tempfile.mkdtemp(prefix="smoke_"))
    pdf = tmp / "smoke.pdf"
    make_paper_pdf(pdf, "Smoke Test Paper",
                   body_facts=["The smoke test trains on 8 NVIDIA P100 GPUs for 3.5 days."],
                   refs=["Vaswani et al. 2017. arXiv:1706.03762"])
    r = c.post(f"{API}/api/papers", files={"file": (pdf.name, io.BytesIO(pdf.read_bytes()),
                                                   "application/pdf")})
    ok = r.status_code == 200
    pid = r.json().get("paper_id") if ok else ""
    job = r.json().get("job_id") if ok else None
    check("upload", ok and bool(pid), f"paper_id={pid}")
    status = "timeout"
    t0 = time.time()
    while job and time.time() - t0 < 300:
        j = c.get(f"{API}/api/jobs/{job}").json()
        status = j.get("status", "?")
        if status in ("done", "error"):
            break
        time.sleep(1)
    check("parse job", status == "done", f"status={status}")

    # ③ outline 按需加载载荷
    r = c.get(f"{API}/api/papers/{pid}/docir?outline=1")
    outline = r.json() if r.status_code == 200 else {}
    check("outline 载荷", r.status_code == 200 and "blocks" not in outline
          and len(outline.get("toc", [])) > 0,
          f"{len(json.dumps(outline))} bytes · toc={len(outline.get('toc', []))}")

    # ④ 按页正文
    r = c.get(f"{API}/api/papers/{pid}/docir?page=1")
    page = r.json() if r.status_code == 200 else {}
    check("按页正文", r.status_code == 200 and len(page.get("blocks", [])) > 0,
          f"blocks={len(page.get('blocks', []))}")

    # ⑤ chat SSE：final 带 claims/anchors（[E 锚点或如实拒答均算链路通）
    r = c.post(f"{API}/api/chat", json={"question": "训练用了几块 GPU？",
                                        "paper_ids": [pid], "conversation_id": "", "agent": "reader"},
               timeout=300)
    final = None
    if r.status_code == 200:
        for ev, data in sse_events(r.iter_lines()):
            if ev == "final":
                final = data
    ok_final = final is not None and ("claims" in final) and ("anchors" in final)
    has_anchor = bool(final and str(final.get("answer", "")).find("[E") >= 0)
    refusal = bool(final and "未找到依据" in str(final.get("answer", "")))
    check("chat SSE 溯源链路", ok_final and (has_anchor or refusal),
          f"锚点引用={has_anchor} · 如实拒答={refusal} · claims={len((final or {}).get('claims', []))}")

    # ⑥ translate 缓存端点（未翻译时 blocks 空 dict 即通）
    r = c.get(f"{API}/api/papers/{pid}/translation?lang=zh")
    check("translate 缓存端点", r.status_code == 200 and isinstance(r.json().get("blocks"), dict))

    # ⑦ terminology 端点
    r = c.post(f"{API}/api/terminology", json={"term": "smoke_term", "translation": "冒烟术语"})
    ok = r.status_code == 200 and any(t["term"] == "smoke_term" for t in r.json().get("terms", []))
    c.delete(f"{API}/api/terminology/smoke_term")
    check("terminology 增删", ok)

    # ⑧ graph 端点
    r = c.get(f"{API}/api/graph")
    g = r.json() if r.status_code == 200 else {}
    check("graph 端点", r.status_code == 200 and "nodes" in g and "insights" in g,
          f"nodes={len(g.get('nodes', []))} edges={g.get('edge_count')}")

    fails = [x for x in RESULTS if not x[1]]
    print(f"\n== 冒烟：{len(RESULTS) - len(fails)}/{len(RESULTS)} PASS ==")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
