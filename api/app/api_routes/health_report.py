"""结果健康评分卡（GET /api/health-report）聚合逻辑 —— 自 routes.py 模块化迁出。

职责：只读聚合最近 7 天本机运行轨迹（trace_events / jobs / qa_logs / papers /
dedup_alerts / docs/acceptance 金标），产出 7 个固定权重子分（缺测不重归一化）
+ 逐维度明细披露。routes.py 保留薄端点；被多端点共用的 _resolve_paper_data
以参数注入（调用时解析 routes 全局名，测试 patch 依旧生效）。

拆分约定：build_health_report 只做编排；各维度聚合拆成独立 _aggregate_* /
_*_quality / _performance / _data_hygiene / _offline_gold 函数——各自只读
自己的表、可单独测试。
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from .. import db, trace_store

_HEALTH_WINDOW_DAYS = 7           # 统计窗口（天）
_HEALTH_MIN_TRACES = 30           # 最小生产样本量；不足时不出总分
_HEALTH_TEST_PREFIXES = (         # 评测/测试轨迹前缀：不计入生产健康分
    "kappa_", "gold_", "bench_", "e2e_", "test_", "demo_",
    "smoke_", "dbg_", "fanout_", "probe", "pytest", "rawsse-test-",
)
_HEALTH_WEIGHTS = {               # 固定权重：缺失维度按 0 计，不做重归一化
    "reliability": 0.20,
    "grounded": 0.20,
    "answer_coverage": 0.05,
    "retrieval": 0.15,
    "parse": 0.15,
    "performance": 0.15,
    "data_health": 0.10,
}


def _clamp01(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 4)


def _wilson_ci(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """比例指标的 Wilson 95% 置信区间（小样本不虚高）。"""
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _is_production_thread(thread_id: str) -> bool:
    """健康分只统计本机运行轨迹：排除离线评测/测试 thread 前缀（无外部用户，如实口径）。"""
    return bool(thread_id) and not thread_id.startswith(_HEALTH_TEST_PREFIXES)


def _percentile(sorted_vals: list[float], p: float) -> float:
    """最近秩百分位（输入需升序；p∈(0,1]）。"""
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, math.ceil(p * len(sorted_vals)) - 1))
    return float(sorted_vals[idx])


def _slo_score(value_ms: float, full_ms: float, zero_ms: float) -> float:
    """延迟 SLO 分段线性：≤full 满分，≥zero 零分，中间线性。"""
    return _clamp01((zero_ms - value_ms) / max(1.0, zero_ms - full_ms))


def _acceptance_dir() -> Path:
    """docs/acceptance 定位（宿主机=仓库根；容器=compose 只读挂载 /app/docs）。
    向上逐级探测而非固定 parents[N]——两种布局零配置生效。"""
    here = Path(__file__).resolve()
    for base in here.parents[:6]:
        cand = base / "docs" / "acceptance"
        if cand.is_dir():
            return cand
    return here.parents[3] / "docs" / "acceptance"


def _latest_eval_gold() -> tuple[str, dict] | None:
    """docs/acceptance 下按 mtime 取最新 eval_gold.json（兼容 round*/ 与 eval-*/*）。"""
    try:
        acc = _acceptance_dir()
        candidates = list(acc.glob("*/eval_gold.json")) + list(acc.glob("*/*/eval_gold.json"))
        candidates = [p for p in candidates if p.is_file()]
        if not candidates:
            return None
        path = max(candidates, key=lambda x: x.stat().st_mtime)
        return path.parent.name, json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 容器内无 docs 目录等场景降级
        return None


# ---------- 维度聚合 helpers（各 helper 只读自己的表，可单独测试） ----------


def _trace_rows(cutoff: float) -> list:
    """① 生产轨迹窗口行：最近 7 天 trace_events，排除评测/测试 thread 前缀。"""
    return [r for r in db.query(
        "SELECT thread_id, status, events_json, updated_at FROM trace_events "
        "WHERE CAST(updated_at AS REAL) >= ? ORDER BY updated_at DESC", (cutoff,))
        if _is_production_thread(str(r["thread_id"] or ""))]


def _aggregate_traces(rows: list) -> dict:
    """① 逐 trace 聚合：失败分类 / plan-replan / verifier 末次裁决 / claims 四态。

    - verifier 每 trace 取末次裁决（避免重试加权）；
    - 失败 trace 区分「可恢复中断（final.resumable=true）」与真实执行失败；
    - v_streak = 自最近一次 verifier 失败以来的连续通过轮数（时间序）。
    """
    claims = {"supported": 0, "needs_check": 0, "unsupported": 0, "absent": 0}
    modes: dict[str, int] = {}
    verifier_pass = verifier_fail = plans = replans = 0
    failed_traces = failed_user_input = answered = 0
    last_fail_ts = None        # 最近一次 verifier 失败时刻（连续通过轮数的锚点）
    v_streak = 0               # 该失败之后的连续通过轮数
    for r in rows:
        if r["status"] == "failed":
            # 失败分类（Round6）：图级异常多为「断点已保存可续跑」的可恢复中断
            # （runner 设计如此），与任务取消同属中断类；仅无法恢复的执行异常
            # 计为可靠性失败。判定：final 无 answer 且 guardrail 含 graph_error。
            try:
                ev = json.loads(r["events_json"] or "[]")
            except ValueError:
                ev = []
            resumable = any(
                isinstance(e, dict) and e.get("kind") == "final"
                and ((e.get("data") or {}).get("resumable") is True) for e in ev)
            if resumable:
                failed_user_input += 1   # 可恢复中断：不计可靠性失败
            else:
                failed_traces += 1
        try:
            events = json.loads(r["events_json"] or "[]")
        except ValueError:
            events = []
        verdict = None
        for e in events:
            if not isinstance(e, dict):
                continue
            if e.get("kind") == "plan":
                plans += 1
                if (e.get("data") or {}).get("round", 1) > 1:
                    replans += 1
            elif e.get("kind") == "verifier":
                verdict = (e.get("data") or {}).get("verdict")
        if verdict == "pass":
            verifier_pass += 1
        elif verdict is not None:
            verifier_fail += 1
            ts = float(r["updated_at"] or 0)
            if last_fail_ts is None or ts > last_fail_ts:
                last_fail_ts = ts
        final = next((e.get("data") or {} for e in reversed(events)
                      if isinstance(e, dict) and e.get("kind") == "final"), {})
        mode = final.get("mode") or ("refusal" if final.get("refusal") else "none")
        modes[mode] = modes.get(mode, 0) + 1
        cs = final.get("claims") or []
        if cs:
            answered += 1
        for c in cs:
            k = c.get("status")
            if k in claims:
                claims[k] += 1
    # 连续通过轮数（时间序）：自最近一次 verifier 失败以来的全部通过轮
    if last_fail_ts is not None:
        for r in rows:
            try:
                events2 = json.loads(r["events_json"] or "[]")
            except ValueError:
                continue
            v2 = next(((e.get("data") or {}).get("verdict") for e in reversed(events2)
                       if isinstance(e, dict) and e.get("kind") == "verifier"), None)
            if v2 == "pass" and float(r["updated_at"] or 0) > last_fail_ts:
                v_streak += 1
    return {
        "n_traces": len(rows), "claims": claims, "modes": modes,
        "verifier_pass": verifier_pass, "verifier_fail": verifier_fail,
        "plans": plans, "replans": replans,
        "failed_traces": failed_traces, "failed_user_input": failed_user_input,
        "answered": answered, "v_streak": v_streak,
        "judged": claims["supported"] + claims["needs_check"] + claims["unsupported"],
    }


def _aggregate_jobs_llm(cutoff: float) -> dict:
    """② 任务失败分类 + LLM 错误率（窗口内）。

    失败分类（Round6 可信度修复）：cancelled（用户主动取消）、运维事件
    （server restarted / 重解析超时被取消）不算可靠性失败——只有真实执行
    失败（解析/索引/生成异常）进可靠性分母；LLM 错误只作展示与审计。
    """
    jrows = db.query("SELECT status, message FROM jobs WHERE CAST(created_at AS REAL) >= ?", (cutoff,))
    jobs_total = len(jrows)
    jobs_cancelled = jobs_infra = 0
    jobs_failed = 0
    for r in jrows:
        if r["status"] in ("cancelled", "cancel_requested"):
            jobs_cancelled += 1
        elif r["status"] in ("error", "failed"):
            msg = (r["message"] or "").lower()
            if "server restarted" in msg or "取消" in (r["message"] or ""):
                jobs_infra += 1
            else:
                jobs_failed += 1
    llm_rows = db.query(
        "SELECT COUNT(*) c FROM qa_logs WHERE CAST(ts AS REAL) >= ?", (cutoff,))[0]["c"]
    llm_errors = db.query(
        "SELECT COUNT(*) c FROM qa_logs WHERE CAST(ts AS REAL) >= ? "
        "AND guardrail LIKE 'llm_error:%'", (cutoff,))[0]["c"]
    return {"jobs_total": jobs_total, "jobs_cancelled": jobs_cancelled,
            "jobs_infra": jobs_infra, "jobs_failed": jobs_failed,
            "llm_rows": llm_rows, "llm_errors": llm_errors,
            "llm_error_rate": _clamp01(llm_errors / llm_rows) if llm_rows else None}


def _parse_quality(resolve_paper_data) -> dict:
    """③ 解析质量：页数加权质量均值 × 质量元数据覆盖率；缺测=0，不假装健康。"""
    papers = db.query("SELECT id, page_count, docir_path FROM papers WHERE docir_path!=''")
    quality_by_paper: list[tuple[int, float]] = []
    cov_vals: list[float] = []
    ref_vals: list[float] = []
    scanned = parse_fail = 0
    page_total = 0
    for pr in papers:
        pages = int(pr["page_count"] or 0) or 1
        page_total += pages
        try:
            data = resolve_paper_data(pr["id"])
        except Exception:  # noqa: BLE001 —— 单篇 DocIR 异常不阻塞整体
            parse_fail += 1
            continue
        if not data:
            parse_fail += 1
            continue
        q = (data.get("meta") or {}).get("quality") or {}
        vals: list[float] = []
        if isinstance(q.get("text_coverage"), (int, float)):
            vals.append(float(q["text_coverage"]))
            cov_vals.append(float(q["text_coverage"]))
        if isinstance(q.get("reference_parse_rate"), (int, float)):
            vals.append(float(q["reference_parse_rate"]))
            ref_vals.append(float(q["reference_parse_rate"]))
        if q.get("scanned_ratio") and float(q["scanned_ratio"]) > 0.5:
            scanned += 1
        if vals:
            quality_by_paper.append((pages, sum(vals) / len(vals)))
    weight_sum = sum(w for w, _ in quality_by_paper)
    page_weighted = (sum(w * v for w, v in quality_by_paper) / weight_sum
                     if weight_sum else None)
    quality_coverage = len(quality_by_paper) / len(papers) if papers else None
    mean_text_coverage = sum(cov_vals) / len(cov_vals) if cov_vals else None
    mean_ref_parse = sum(ref_vals) / len(ref_vals) if ref_vals else None
    parse_value = None
    if page_weighted is not None and quality_coverage is not None:
        parse_value = _clamp01(page_weighted * quality_coverage)
    parse = None
    if papers:
        parse = {
            "value": parse_value,
            "page_weighted": _clamp01(page_weighted) if page_weighted is not None else None,
            "quality_coverage": (_clamp01(quality_coverage)
                                 if quality_coverage is not None else None),
            "mean_text_coverage": (_clamp01(mean_text_coverage)
                                   if mean_text_coverage is not None else None),
            "mean_ref_parse": _clamp01(mean_ref_parse) if mean_ref_parse is not None else None,
            "papers_with_quality": len(quality_by_paper),
            "papers_total": len(papers),
            "scanned_papers": scanned,
            "parse_fail": parse_fail,
            "page_total": page_total,
        }
    return {"papers": papers, "parse": parse, "parse_value": parse_value,
            "page_weighted": page_weighted,
            "mean_text_coverage": mean_text_coverage,
            "mean_ref_parse": mean_ref_parse,
            "quality_by_paper": quality_by_paper,
            "scanned": scanned, "parse_fail": parse_fail}


def _retrieval_dense() -> dict:
    """③′ 检索健康（仅披露不计分）：dense 向量覆盖率。

    BM25 对纯中文查询零召回，dense 是中文维度唯一检索通道；入库时 embedding
    失败会让整篇 vec IS NULL 且无任何告警——静默损伤必须变成可见指标。
    """
    dense_cov = None
    dense_papers_low = []
    try:
        cov_rows = db.query(
            "SELECT p.id, COUNT(c.id) n, "
            "SUM(CASE WHEN c.vec IS NOT NULL THEN 1 ELSE 0 END) v "
            "FROM papers p JOIN chunks c ON c.paper_id=p.id "
            "WHERE p.docir_path!='' GROUP BY p.id HAVING n>0")
        if cov_rows:
            tot = sum(r["n"] for r in cov_rows)
            have = sum(r["v"] or 0 for r in cov_rows)
            dense_cov = round(have / tot, 3) if tot else None
            dense_papers_low = [r["id"][:16] for r in cov_rows
                                if tot and (r["v"] or 0) < r["n"]]
    except Exception:  # noqa: BLE001 —— 指标披露失败不影响健康报告
        dense_cov = None
    return {
        "dense_vec_coverage": dense_cov,
        "papers_missing_vec": len(dense_papers_low),
        "note": "BM25 纯中文零召回——dense 是中文检索唯一通道；覆盖率<1 的论文中文提问检索降级",
        "healthy": dense_cov is not None and dense_cov >= 0.95,
    }


def _memory_disclosure() -> dict | None:
    """模块七记忆子系统只读披露（不入 7 子分权重；披露失败不影响健康报告）。"""
    try:
        mem_total = db.query("SELECT COUNT(*) c FROM memory_events")[0]["c"]
        mem_hyp = db.query(
            "SELECT COUNT(*) c FROM memory_events WHERE kind='hypothesis'")[0]["c"]
        mem_hyp_active = db.query(
            "SELECT COUNT(*) c FROM memory_events WHERE kind='hypothesis' "
            "AND status IN ('proposed','active')")[0]["c"]
        mem_edges = db.query(
            "SELECT COUNT(*) c FROM memory_edges WHERE status='active'")[0]["c"]
        mem_stale = db.query(
            "SELECT COUNT(*) c FROM memory_events "
            "WHERE status IN ('superseded','refuted')")[0]["c"]
        mem_llm = db.query(
            "SELECT COUNT(*) c FROM memory_events WHERE source='llm'")[0]["c"]
        return {
            "events": mem_total,
            "hypotheses": mem_hyp,
            "hypotheses_active": mem_hyp_active,
            "active_edges": mem_edges,
            "stale_events": mem_stale,
            "llm_extract_share": round(mem_llm / mem_total, 3) if mem_total else None,
            "note": "记忆子系统只读披露（模块七，不入 7 子分权重）；"
                    "抽取 LLM 占比低 = 规则兜底口径在生效（如实呈现）",
            "healthy": mem_total > 0,
        }
    except Exception:  # noqa: BLE001 —— 指标披露失败不影响健康报告
        return None


def _performance(cutoff: float) -> dict:
    """④ 性能：窗口内问答调用 p50/p95 SLO + 每模式分列（样本 <20 不出分位数）。"""
    lat_rows = db.query(
        "SELECT latency_ms FROM qa_logs WHERE CAST(ts AS REAL) >= ? "
        "AND module IN ('qa', 'chat') AND latency_ms > 0 ORDER BY latency_ms", (cutoff,))
    lat = [float(r["latency_ms"]) for r in lat_rows]
    p50 = _percentile(lat, 0.50) if len(lat) >= 20 else None
    p95 = _percentile(lat, 0.95) if len(lat) >= 20 else None
    avg_lat = sum(lat) / len(lat) if lat else 0.0
    performance = None
    if p50 is not None and p95 is not None:
        performance = _clamp01(0.5 * _slo_score(p50, 5000, 20000)
                               + 0.5 * _slo_score(p95, 25000, 100000))
    # 每模式 SLO（Round6-P1）：专业/普通文档延迟分列（qa_logs.doc_mode，
    # runner 线程内落库）；样本 <10 条的模式不出分位数，如实标注
    perf_by_mode: dict[str, dict] = {}
    for mrow in db.query(
            "SELECT COALESCE(NULLIF(doc_mode,''),'unknown') AS dm, latency_ms "
            "FROM qa_logs WHERE CAST(ts AS REAL) >= ? "
            "AND module IN ('qa', 'chat') AND latency_ms > 0", (cutoff,)):
        perf_by_mode.setdefault(mrow["dm"], []).append(float(mrow["latency_ms"]))
    mode_slo = {dm: {"n": len(v),
                     "p50_ms": int(_percentile(sorted(v), 0.50)) if len(v) >= 10 else None,
                     "p95_ms": int(_percentile(sorted(v), 0.95)) if len(v) >= 10 else None}
                for dm, v in perf_by_mode.items()}
    return {"lat": lat, "p50": p50, "p95": p95, "avg_lat": avg_lat,
            "performance": performance, "mode_slo": mode_slo}


def _data_hygiene(cutoff: float, parse_fail: int, papers_total: int) -> dict:
    """⑤ 数据卫生：陈旧去重积压 + 解析失败（新鲜 pending 属正常待办不扣分）。"""
    dedup_rows = db.query("SELECT status, COUNT(*) c FROM dedup_alerts GROUP BY status")
    dedup = {r["status"]: r["c"] for r in dedup_rows}
    dedup_total = sum(dedup.values())
    dedup_pending = dedup.get("pending", 0)
    stale_pending = db.query(
        "SELECT COUNT(*) c FROM dedup_alerts WHERE status='pending' "
        "AND CAST(created_at AS REAL) < ?", (cutoff,))[0]["c"]
    dedup_fresh = 1.0
    if dedup_total:
        dedup_fresh = 1 - stale_pending / dedup_total
    parse_ok = 1.0
    if papers_total:
        parse_ok = 1 - parse_fail / papers_total
    return {"value": _clamp01(dedup_fresh * parse_ok), "dedup": dedup,
            "dedup_total": dedup_total, "dedup_pending": dedup_pending,
            "stale_pending": stale_pending}


def _offline_gold() -> tuple[dict, float | None]:
    """⑥ 离线金标：最新 eval_gold gates 全过→不封顶；否则总分封顶 0.90。"""
    eval_info = _latest_eval_gold()
    offline: dict[str, Any] = {"available": False}
    external_cap = None
    if eval_info:
        round_name, egr = eval_info
        gen = egr.get("generative") or {}
        gates = egr.get("gates") or {}
        gates_total = len(gates)
        gates_failed = [k for k, v in gates.items() if not v]
        gates_passed = gates_total - len(gates_failed)
        external_cap = 1.0 if (gates_total and not gates_failed) else 0.90
        offline = {"available": True, "round": round_name,
                   "date": str(egr.get("time", ""))[:10],
                   "gates_passed": gates_passed, "gates_total": gates_total,
                   "gates_failed": gates_failed,
                   "hallucination_rate": gen.get("hallucination_rate"),
                   "supported_ratio": gen.get("supported_ratio"),
                   "anchor_precision": gen.get("anchor_precision"),
                   "quote_unverified_rate": gen.get("quote_unverified_rate"),
                   "recall_at5": egr.get("recall_at5"),
                   "mrr_at5": egr.get("mrr_at5"),
                   "refusal_accuracy": egr.get("refusal_accuracy")}
    return offline, external_cap


def build_health_report(resolve_paper_data):
    """结果健康评分卡（只读聚合；口径与阈值见 docs/api.md）。

    2026-11-15 重写要点：
    - 窗口 + 样本：最近 7 天、只统计本机运行轨迹（排除评测/测试 thread 前缀；无外部用户，如实口径）；
      样本 < 30 条时 total 置空（insufficient），避免小样本高分。
    - 失败惩罚：失败 trace / 失败任务进入可靠性分母；拒答/无答案轨迹单列"回答覆盖"。
    - 缺测不重归一化：固定权重，缺失维度按 0 计并列入 missing。
    - 不自证循环：grounded 仅 claims 四态；检索=离线 recall@5 + 在线 verifier 各半；
      性能=窗口 p50/p95 SLO；数据卫生=去重积压/解析失败。
    - 外部校准：最近一轮 eval_gold gates 未全过时总分封顶 0.90，并返回失败项。
    - 工程健康（pytest/e2e）由前端 evidence.ts 单独展示，不并入运行时健康分。
    """
    now = time.time()
    cutoff = now - _HEALTH_WINDOW_DAYS * 86400
    trace_store.init()   # 幂等：确保 trace_events 存在（新库/测试库首访不 500）

    # ---- ① 生产轨迹窗口（逐 trace 聚合；verifier 每 trace 取末次，避免重试加权） ----
    rows = _trace_rows(cutoff)
    tr = _aggregate_traces(rows)
    n_traces = tr["n_traces"]
    claims = tr["claims"]
    modes = tr["modes"]
    verifier_pass, verifier_fail = tr["verifier_pass"], tr["verifier_fail"]
    plans, replans = tr["plans"], tr["replans"]
    failed_traces, failed_user_input = tr["failed_traces"], tr["failed_user_input"]
    answered, v_streak = tr["answered"], tr["v_streak"]
    judged = tr["judged"]

    # ---- ② 任务失败 / LLM 调用错误（窗口内；LLM 错误作展示与审计，不重复扣分） ----
    jl = _aggregate_jobs_llm(cutoff)
    jobs_total, jobs_cancelled = jl["jobs_total"], jl["jobs_cancelled"]
    jobs_infra, jobs_failed = jl["jobs_infra"], jl["jobs_failed"]
    llm_rows, llm_errors = jl["llm_rows"], jl["llm_errors"]
    llm_error_rate = jl["llm_error_rate"]

    # ---- ③ 解析质量（页数加权均值 × 质量元数据覆盖率；缺测=0，不假装健康） ----
    pq = _parse_quality(resolve_paper_data)
    papers, parse = pq["papers"], pq["parse"]
    parse_value, page_weighted = pq["parse_value"], pq["page_weighted"]
    mean_text_coverage, mean_ref_parse = pq["mean_text_coverage"], pq["mean_ref_parse"]
    quality_by_paper = pq["quality_by_paper"]
    scanned, parse_fail = pq["scanned"], pq["parse_fail"]

    # ---- ③′ 检索健康（Round6-P1 新增，仅披露不计分）：dense 向量覆盖 ----
    retrieval_health = _retrieval_dense()

    # ---- 模块七：记忆子系统只读披露（不入 7 子分权重，模块明细区展示） ----
    memory_health = _memory_disclosure()

    # ---- ④ 性能（窗口内问答 LLM 调用：p50/p95 SLO；均值只作明细） ----
    pf = _performance(cutoff)
    lat, p50, p95 = pf["lat"], pf["p50"], pf["p95"]
    avg_lat, performance = pf["avg_lat"], pf["performance"]
    mode_slo = pf["mode_slo"]

    # ---- ⑤ 数据卫生（陈旧去重积压 + 解析失败；新鲜的 pending 是正常待办不扣分） ----
    dh = _data_hygiene(cutoff, parse_fail, len(papers))
    data_health = dh["value"]
    dedup, dedup_total = dh["dedup"], dh["dedup_total"]
    dedup_pending, stale_pending = dh["dedup_pending"], dh["stale_pending"]

    # ---- ⑥ 离线金标（最新 eval_gold；gates 未全过时总分封顶 0.90） ----
    offline, external_cap = _offline_gold()

    # ---- ⑦ 子分（固定权重；缺失=0 并列入 missing，不重归一化） ----
    scores: dict[str, dict] = {}

    def add_score(key: str, value: float | None, formula: str, raw: dict,
                  n: int | None = None, ci: list[float] | None = None) -> None:
        if value is None:
            return
        item: dict[str, Any] = {"value": _clamp01(value), "weight": _HEALTH_WEIGHTS[key],
                                "formula": formula, "raw": raw}
        if n is not None:
            item["n"] = n
        if ci is not None:
            item["ci"] = ci
        scores[key] = item

    attempts = n_traces + jobs_total
    # 失败分类（Round6）：cancelled/运维事件（server restarted 等）不进可靠性分母，
    # 只统计真实执行失败——「重解析超时被取消」「重启自愈」不是系统可靠性问题。
    attempts_real = attempts - jobs_cancelled - jobs_infra
    reliability = (_clamp01(1 - (failed_traces + jobs_failed) / attempts_real)
                   if attempts_real else None)
    add_score(
        "reliability", reliability,
        f"1 - 失败（trace {failed_traces}/{n_traces} + 任务 {jobs_failed}/{jobs_total}）"
        f" · 非失败排除：取消 {jobs_cancelled} / 运维 {jobs_infra}"
        f" · LLM 错误 {llm_errors}/{llm_rows}"
        + (f"（{llm_error_rate:.1%}）" if llm_error_rate is not None else "")
        + f" · 窗口 {_HEALTH_WINDOW_DAYS} 天",
        {"failed_traces": failed_traces, "traces": n_traces,
         "jobs_failed": jobs_failed, "jobs": jobs_total,
         "jobs_cancelled": jobs_cancelled, "jobs_infra": jobs_infra,
         "traces_resumable": failed_user_input,
         "llm_errors": llm_errors, "llm_calls": llm_rows},
        n=attempts_real,
        ci=_wilson_ci(attempts_real - failed_traces - jobs_failed, attempts_real) if attempts_real else None)

    grounded = _clamp01(claims["supported"] / judged) if judged else None
    add_score(
        "grounded", grounded,
        f"supported {claims['supported']} / 判定论断 {judged}"
        f"（absent {claims['absent']} 诚实负向不计入分母；失败/无答案见可靠性/回答覆盖）",
        claims, n=judged, ci=_wilson_ci(claims["supported"], judged) if judged else None)

    coverage = _clamp01(answered / n_traces) if n_traces else None
    add_score(
        "answer_coverage", coverage,
        f"有论断轨迹 {answered}/{n_traces}（no_answer {n_traces - answered}；"
        f"拒答 {modes.get('refusal', 0)} / 抽取式 {modes.get('extractive', 0)}）",
        {"answered": answered, "no_answer": n_traces - answered, "modes": modes},
        n=n_traces, ci=_wilson_ci(answered, n_traces) if n_traces else None)

    online_verifier = (_clamp01(verifier_pass / (verifier_pass + verifier_fail))
                       if (verifier_pass + verifier_fail) else None)
    offline_recall = (float(offline["recall_at5"])
                      if offline.get("available")
                      and isinstance(offline.get("recall_at5"), (int, float)) else None)
    rparts = [v for v in (online_verifier, offline_recall) if v is not None]
    retrieval = _clamp01(sum(rparts) / len(rparts)) if rparts else None
    add_score(
        "retrieval", retrieval,
        f"在线 verifier 通过 {verifier_pass}/{verifier_pass + verifier_fail}"
        + (f" · 自最近一次失败已连续通过 {v_streak} 轮"
           if v_streak else "")
        + (f" · 离线 recall@5 {offline_recall:.3f}"
           if offline_recall is not None else "")
        + f"（补检索 {replans}/{plans} 轮）",
        {"verifier_pass": verifier_pass, "verifier_fail": verifier_fail,
         "verifier_streak": v_streak,
         "offline_recall_at5": offline_recall, "plans": plans, "replans": replans},
        n=verifier_pass + verifier_fail)

    add_score(
        "parse", parse_value,
        (f"页数加权质量 {page_weighted:.3f} × 质量元数据覆盖 "
         f"{len(quality_by_paper)}/{len(papers)} 篇"
         if page_weighted is not None
         else f"页数加权质量 — × 质量元数据覆盖 {len(quality_by_paper)}/{len(papers)} 篇")
        + (f" · 文本覆盖 {mean_text_coverage:.3f}" if mean_text_coverage is not None else "")
        + (f" · ref 解析 {mean_ref_parse:.3f}" if mean_ref_parse is not None else ""),
        {"page_weighted": parse["page_weighted"] if parse else None,
         "quality_coverage": parse["quality_coverage"] if parse else None,
         "papers_with_quality": len(quality_by_paper), "papers_total": len(papers),
         "scanned_papers": scanned, "parse_fail": parse_fail},
        n=len(quality_by_paper))

    add_score(
        "performance", performance,
        (f"p50 {int(p50)}ms（SLO 5000→20000ms）· p95 {int(p95)}ms"
         f"（SLO 25000→100000ms，含深度思考）· n={len(lat)}")
        if p50 is not None and p95 is not None else "窗口内问答调用样本 <20",
        {"p50_ms": int(p50) if p50 is not None else None,
         "p95_ms": int(p95) if p95 is not None else None,
         "avg_ms": round(avg_lat), "count": len(lat)},
        n=len(lat))

    add_score(
        "data_health", data_health,
        f"陈旧去重积压 {stale_pending}/{dedup_total} · 解析失败 {parse_fail}/{len(papers)} 篇"
        f"（新鲜 pending {dedup_pending - stale_pending} 条属正常待办，不扣分）",
        {"stale_pending": stale_pending, "dedup_total": dedup_total,
         "dedup_pending": dedup_pending, "parse_fail": parse_fail,
         "papers_total": len(papers)}, n=dedup_total)

    missing = [k for k in _HEALTH_WEIGHTS if k not in scores]
    runtime_total = round(sum(s["value"] * s["weight"] for s in scores.values()), 4)
    insufficient = n_traces < _HEALTH_MIN_TRACES
    insufficient_reason = ""
    total = None
    cap_applied = False
    if insufficient:
        insufficient_reason = (f"窗口内生产轨迹 {n_traces} 条 < 最小样本 "
                               f"{_HEALTH_MIN_TRACES} 条，不出总分（明细仍展示）")
    else:
        total = _clamp01(runtime_total)
        if external_cap is not None and total > external_cap:
            total = external_cap
            cap_applied = True

    return {
        "total": total,
        "runtime_total": _clamp01(runtime_total),
        "cap": external_cap,
        "cap_applied": cap_applied,
        "insufficient": insufficient,
        "insufficient_reason": insufficient_reason,
        "window": {"days": _HEALTH_WINDOW_DAYS, "cutoff": round(cutoff, 3),
                   "min_traces": _HEALTH_MIN_TRACES, "traces": n_traces},
        "weights": _HEALTH_WEIGHTS,
        "missing": missing,
        "scores": scores,
        "samples": {"traces": n_traces, "failed": failed_traces,
                    "resumable": failed_user_input,
                    "answered": answered, "no_answer": n_traces - answered,
                    "modes": modes, "jobs": jobs_total, "jobs_failed": jobs_failed,
                    "llm_calls": llm_rows, "llm_errors": llm_errors,
                    "llm_error_rate": llm_error_rate},
        "trace": {"samples": n_traces, "claims": claims,
                  "verifier_pass": verifier_pass, "verifier_fail": verifier_fail,
                  "plans": plans, "replans": replans, "modes": modes},
        "qa_perf": {"count": len(lat), "avg_latency_ms": round(avg_lat),
                    "p50_ms": int(p50) if p50 is not None else None,
                    "p95_ms": int(p95) if p95 is not None else None,
                    "error_rate": (failed_traces / n_traces) if n_traces else None,
                    "by_mode": mode_slo},
        "parse": parse,
        "retrieval_health": retrieval_health,
        "memory_health": memory_health,
        "dedup": {"total": dedup_total, "pending": dedup_pending,
                  "stale_pending": stale_pending,
                  "covered": dedup.get("covered", 0), "kept": dedup.get("kept", 0),
                  "coexisted": dedup.get("coexisted", 0)},
        "offline": offline,
        "external": {**offline, "cap": external_cap} if offline.get("available") else offline,
        # Round6 可信度披露：judge 校准状态（κ 如实，未达 0.6 前明确标注）+
        # 金标三轮趋势（评测轮次关键指标，历史来源 docs/acceptance/）
        "calibration": _calibration(),
        "trend": _eval_trend(),
    }


def _calibration() -> dict:
    """judge 校准披露（读 eval_kappa.py 产出的 kappa_report.json 实测值，
    不再硬编码——报告没跑过/缺文件时如实标注「未评测」而非展示旧数字）。
    P1 修订：披露必须带 n 与 95%CI（点估计 alone 是过度宣称），并明示
    「正类来自管线自身产物、非独立人工标注」。"""
    report_path = Path(__file__).resolve().parents[2] / "scripts" / "kappa_report.json"
    try:
        d = json.loads(report_path.read_text(encoding="utf-8"))
        kj, kp = d.get("cohens_kappa"), d.get("pipeline_kappa")
        ok = (isinstance(kj, (int, float)) and kj >= 0.60
              and isinstance(kp, (int, float)) and kp >= 0.60)
        note = "judge-only κ（L4 判别力）+ pipeline κ（L2-L4 端到端防御），" \
               "构造真值口径，目标双 κ≥0.60"
        if not ok:
            note += "；未达标——校准进行中"
        return {"judge_kappa": kj, "pipeline_kappa": kp, "kappa_note": note,
                "n": d.get("n_evaluated"),
                "judge_ci95": d.get("kappa_ci95"),
                "pipeline_ci95": d.get("pipeline_kappa_ci95"),
                "ci_note": d.get("ci_note", ""),
                "caveat": "正类来自管线自身产物，非独立人工标注——负类为构造校准口径",
                "evaluated_at": d.get("time", ""),
                "grounded_note": "回答可信=claims 四态口径；supported 已附逐字原文引文"}
    except Exception:  # noqa: BLE001 —— 无报告=未评测，如实披露
        return {"judge_kappa": None, "pipeline_kappa": None,
                "kappa_note": "评测未运行（scripts/eval_kappa.py 无报告产出）——如实披露，不展示历史数字",
                "n": None, "judge_ci95": None, "pipeline_ci95": None,
                "ci_note": "",
                "caveat": "正类来自管线自身产物，非独立人工标注——负类为构造校准口径",
                "evaluated_at": "",
                "grounded_note": "回答可信=claims 四态口径；supported 已附逐字原文引文"}


def _eval_trend() -> list[dict]:
    """金标评测轮次趋势（docs/acceptance 下全部 eval_gold.json 按 mtime 升序）。"""
    try:
        acc = _acceptance_dir()
        out = []
        for path in sorted(list(acc.glob("*/eval_gold.json")) + list(acc.glob("*/*/eval_gold.json")),
                           key=lambda x: x.stat().st_mtime):
            d = json.loads(path.read_text(encoding="utf-8"))
            g = d.get("generative", {})
            out.append({
                "dir": path.parent.name,
                "recall_at5": d.get("recall_at5"),
                "hallucination_rate": g.get("hallucination_rate"),
                "anchor_precision": g.get("anchor_precision"),
            })
        return out[-6:]
    except Exception:  # noqa: BLE001 —— 趋势读取失败不影响健康报告
        return []

