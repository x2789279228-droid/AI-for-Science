"""记忆巩固 worker（模块七 P1，清单 §三#9）：仿 tracker/recovery——daemon 线程 +
`_stop.wait(interval)` 睡眠循环 + 退出 Event + `interval<=0` 关闭（不引入 APScheduler）。

周期任务：
1) FadeMem 强度重算 + 滞回层迁移（drift.recalculate_strengths，只改排序乘子不删数据）；
2) 假设 FSM 复核：proposed → active（出现同主题新证据，verdict_source=evidence）；
   同主题更新假设出现 → 旧 proposed/active 标 superseded（refuted/supported 判定留给
   briefing 显式触发——判定源可控可审计，worker 不代行 judge）；
3) 简报缓存预热：cached_brief 用户级 + 最近 3 个活跃会话级（judge 回填线程化不阻塞）。
"""
from __future__ import annotations

import logging
import threading

from ..config import settings
from ..db import query as db_query
from . import briefing, drift, store

log = logging.getLogger("meridian.memory")

_stop = threading.Event()
_thread: threading.Thread | None = None

# 同主题假设判定：内容 bigram 包含度 ≥ 0.35（与 briefing._SIM_THRESHOLD 同族口径）
_HYP_SIM_THRESHOLD = 0.35


def start_memory_worker() -> bool:
    """启动巩固 worker；interval<=0 返回 False（关闭口径，仿 tracking worker）。"""
    global _thread
    if settings.memory_worker_interval_s <= 0:
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(settings.memory_worker_interval_s,),
                               daemon=True, name="memory-worker")
    _thread.start()
    return True


def stop_memory_worker() -> None:
    _stop.set()
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout=3.0)


def _loop(interval: int) -> None:
    # 先等后跑：启动即跑会拖慢首请求与测试（tracker 同款约定）
    while not _stop.wait(interval):
        try:
            summary = run_once()
            log.info("巩固 worker 一轮完成: %s", summary)
        except Exception as e:  # noqa: BLE001 —— 周期任务失败不中断循环
            log.warning("巩固 worker 一轮失败: %s: %s", type(e).__name__, e)


def _review_hypotheses(*, user_id: str = "local") -> dict:
    """FSM 复核（清单 §2.2 触发器）：proposed→active 与同主题 superseded。"""
    promoted = superseded = 0
    hyps = [h for h in store.list_events(kind="hypothesis", limit=100)
            if h["user_id"] == user_id and h["status"] in ("proposed", "active")]
    hyps.sort(key=lambda h: h["ts"])          # 旧→新
    for i, h in enumerate(hyps):
        evidence = briefing.collect_new_evidence(h, user_id=user_id)
        if h["status"] == "proposed" and evidence:
            try:
                store.transition(h["id"], "active",
                                 why="worker 复核：出现同主题新证据", verdict_source="evidence")
                promoted += 1
            except store.FSMError:
                pass
        # 同主题更新假设 → 旧假设 superseded（append-only 演化史）
        for newer in hyps[i + 1:]:
            if newer["status"] not in ("proposed", "active"):
                continue
            from .briefing import _sim_to_hyp
            if _sim_to_hyp(h, newer["content"]) >= _HYP_SIM_THRESHOLD:
                store.supersede_event(h["id"], by_id=newer["id"],
                                      why="worker 复核：同主题更新假设提出", source="agent")
                superseded += 1
                break
    return {"promoted_to_active": promoted, "superseded": superseded}


def run_once(*, user_id: str = "local") -> dict:
    """单轮巩固（可独立调用，测试与手动触发用）。"""
    strengths = drift.recalculate_strengths(user_id=user_id)
    review = _review_hypotheses(user_id=user_id)
    warmed = 1
    briefing.cached_brief(user_id=user_id)                 # 用户级
    rows = db_query("SELECT DISTINCT conversation_id AS c FROM memory_events "
                    "WHERE user_id=? AND conversation_id!='' "
                    "ORDER BY rowid DESC LIMIT 3", (user_id,))
    for r in rows:
        briefing.cached_brief(conversation_id=r["c"], user_id=user_id)
        warmed += 1
    return {"strengths": strengths, "review": review, "briefs_warmed": warmed}
