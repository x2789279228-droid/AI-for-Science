"""Round4 模块四：外部 API live 探针（宿主机跑，产物 live_probe.json）。

对 arXiv Atom / Semantic Scholar / OpenAlex 各发 ≥3 次真实调用：
- 走仓库同款 provider 函数（discovery._arxiv/_s2/_openalex_provider）——测的就是
  生产代码路径（含 301 跟随、429 单次退避、限速参数）；
- 记录每次：查询词、返回条数、延迟、成功/失败与错误摘要；
- search_multi 三源并行 + 三索引去重一次（记录去重前后与 per_source）；
- 超时行为：httpx 以 15ms 超时直连 arXiv 端点 → 记录超时异常类型（对照生产
  timeout=25~40s 设置的必要性，此为对照实验非生产行为）；
- 离线降级：monkeypatch discovery.httpx 抛 ConnectError → search_multi 必须
  优雅返回（不抛异常；per_source 如实 ok=false）。

用法：cd api && uv run python scripts/live_probe.py --out ../../docs/acceptance/round4-<date>/
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


_QUERIES = {
    "arxiv": ["transformer attention", "scaling laws language models", "retrieval augmented generation"],
    "s2": ["attention is all you need", "bert pre-training", "gpt-3 few-shot"],
    "openalex": ["vision transformer", "instruction tuning", "mixture of experts"],
}


def probe_providers() -> dict:

    from app.rag import discovery

    out: dict[str, list] = {}
    for name, queries in _QUERIES.items():
        fn = getattr(discovery, f"_{name}_provider")
        rows = []
        for q in queries:
            t0 = time.time()
            try:
                r = fn(q, 5)
                rows.append({"query": q, "ok": bool(r.get("ok")),
                             "entries": len(r.get("entries") or []),
                             "latency_ms": round((time.time() - t0) * 1000),
                             "error": (r.get("error") or "")[:120]})
            except Exception as e:  # noqa: BLE001 —— 记录即目的
                rows.append({"query": q, "ok": False, "entries": 0,
                             "latency_ms": round((time.time() - t0) * 1000),
                             "error": f"{type(e).__name__}: {e}"[:120]})
            print(f"  [{name}] {'✓' if rows[-1]['ok'] else '✗'} {q[:30]}"
                  f" · {rows[-1]['entries']} 条 · {rows[-1]['latency_ms']}ms")
        out[name] = rows
    return out


def probe_multi() -> dict:
    from app.rag import discovery

    t0 = time.time()
    r = discovery.search_multi("transformer attention mechanisms", limit=8)
    return {"ok": bool(r["entries"]), "entries": len(r["entries"]),
            "per_source": r["per_source"], "latency_ms": round((time.time() - t0) * 1000)}


def probe_timeout() -> dict:
    """对照实验：15ms 超时直连 arXiv（生产 provider timeout=30s，此处验证远端延迟
    真实存在、超时异常类型符合预期——适配器的超时/回退配置不是摆设）。"""
    import httpx

    t0 = time.time()
    try:
        httpx.get("https://export.arxiv.org/api/query",
                  params={"search_query": "all:transformer", "max_results": 1},
                  timeout=0.015, follow_redirects=True)
        return {"raised": False, "latency_ms": round((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"raised": True, "exc": type(e).__name__,
                "latency_ms": round((time.time() - t0) * 1000)}


def probe_offline() -> dict:
    """离线降级：httpx 抛连接错误 → search_multi 优雅返回（不冒充、不崩溃）。

    同时替换 discovery.httpx（S2/OpenAlex 直连）与 agents.tools.httpx
    （_arxiv_search）——三源网络层全断，验证 per-source 失败聚合与缓存回放标注。
    """
    from app.agents import tools as agent_tools
    from app.rag import discovery

    class _Offline:
        def get(self, *a, **k):
            raise ConnectionError("offline-probe")

        def post(self, *a, **k):
            raise ConnectionError("offline-probe")

    orig_d, orig_t = discovery.httpx, agent_tools.httpx
    discovery.httpx = _Offline()        # type: ignore[assignment]
    agent_tools.httpx = _Offline()      # type: ignore[assignment]
    try:
        r = discovery.search_multi("transformer attention", limit=5)
        per = r.get("per_source", {})
        return {"graceful": True, "entries": len(r.get("entries") or []),
                "per_source": per,
                "sources_labeled": {k: v.get("source") for k, v in per.items()},
                # 优雅口径：断网下不应有任何 live 源（cache_offline/none 如实标注）
                "no_live_source": all(v.get("source") != "live" for v in per.values())}
    except Exception as e:  # noqa: BLE001 —— 抛异常=降级失败（FAIL）
        return {"graceful": False, "error": f"{type(e).__name__}: {e}"[:160]}
    finally:
        discovery.httpx = orig_d
        agent_tools.httpx = orig_t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="../../docs/acceptance/_liveprobe")
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    print("== provider 直连（生产代码路径）==")
    providers = probe_providers()
    print("== search_multi 三源并行去重 ==")
    multi = probe_multi()
    print(f"  entries={multi['entries']} per_source="
          + json.dumps({k: v.get("ok") for k, v in multi["per_source"].items()}))
    print("== 超时对照（15ms）==")
    timeout = probe_timeout()
    print(f"  raised={timeout.get('raised')} {timeout.get('exc', '')}")
    print("== 离线降级 ==")
    offline = probe_offline()
    print(f"  graceful={offline.get('graceful')} no_live_source={offline.get('no_live_source')}")

    # 门禁口径：单源 429 是共享池常态（S2 实测三连 429）——多源冗余正是设计答案，
    # 故 gate = ≥2/3 源各自有成功调用 + multi 端到端有产出 + 离线优雅回放；
    # all-3 严格口径单独如实记录（不静默放宽、不冒充）。
    src_ok = [any(r["ok"] for r in rows) for rows in providers.values()]
    report = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "providers": providers,
        "provider_sources_ok": src_ok,
        "all3_sources_ok_strict": all(src_ok),
        "search_multi": multi,
        "timeout_probe": timeout,
        "offline_probe": offline,
        "ok": bool(sum(src_ok) >= 2 and multi.get("ok") and offline.get("graceful")
                   and offline.get("no_live_source")),
    }
    (outdir / "live_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    print(f"\n== live probe {'PASS' if report['ok'] else 'FAIL'} ==")
    print(f"已写出：{outdir / 'live_probe.json'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
