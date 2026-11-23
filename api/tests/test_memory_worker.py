"""模块七 P1：巩固 worker 单测（全离线）——run_once 三任务 + daemon 生命周期。

对应清单 §三#9：强度重算 / 假设 FSM 复核（proposed→active 与同主题 superseded）/
简报缓存预热；start/stop 仿 tracker（interval<=0 关闭）。
"""
from __future__ import annotations

import time
from datetime import datetime

from app.config import settings
from app.memory import briefing, store, worker
from app.memory.timeparse import LOCAL_TZ

TZ = LOCAL_TZ
T0 = datetime(2026, 11, 1, tzinfo=TZ).timestamp()
U = "u_worker"


def test_run_once_promotes_proposed_to_active():
    h = store.add_event("hypothesis", "我假设巩固 worker 能提升假设状态",
                        user_id=U, conversation_id="wk1", turn=1,
                        thread_id="wk1:1", ts=T0, source="rule")
    store.add_event("experiment_result", "巩固 worker 的实验验证记录已出现",
                    user_id=U, conversation_id="wk1", turn=2, thread_id="wk1:2",
                    ts=T0 + 86400, source="rule")
    out = worker.run_once(user_id=U)
    assert out["review"]["promoted_to_active"] >= 1
    assert store.get_event(h["id"])["status"] == "active"
    assert out["strengths"]["updated"] >= 2, "强度重算必须覆盖本轮事件"
    assert briefing._BRIEF_CACHE, "简报缓存必须已预热"


def test_run_once_supersedes_older_same_topic_hypothesis():
    old = store.add_event("hypothesis", "我假设批处理是数据管道的最优解",
                          user_id=U, conversation_id="wk2", turn=1, ts=T0, source="rule")
    new = store.add_event("hypothesis", "我修正假设：流式处理才是数据管道的最优解取代旧解",
                          user_id=U, conversation_id="wk2", turn=2, ts=T0 + 7 * 86400,
                          source="rule")
    worker.run_once(user_id=U)
    assert store.get_event(old["id"])["status"] == "superseded"
    assert store.get_event(new["id"])["status"] in ("proposed", "active")
    assert store.get_event(old["id"])["meta"].get("superseded_by") == new["id"]


def test_worker_does_not_judge_verdicts():
    """worker 只做 proposed→active 与 superseded，不代行 refuted/supported 判定
    （判定源留给 briefing 显式触发，清单 §三#9 边界）。"""
    h = store.add_event("hypothesis", "我假设评测协议需要修正",
                        user_id=U, conversation_id="wk3", turn=1, ts=T0, source="rule")
    worker.run_once(user_id=U)
    assert store.get_event(h["id"])["status"] in ("proposed", "active"), \
        "worker 不得给出 refuted/supported 终态判定"


def test_start_disabled_when_interval_zero(monkeypatch):
    monkeypatch.setattr(settings, "memory_worker_interval_s", 0)
    assert worker.start_memory_worker() is False, "interval<=0 必须关闭（仿 tracker）"
    worker.stop_memory_worker()                       # 幂等


def test_start_stop_lifecycle(monkeypatch):
    monkeypatch.setattr(settings, "memory_worker_interval_s", 1)
    try:
        assert worker.start_memory_worker() is True
        assert worker._thread is not None and worker._thread.is_alive()
        time.sleep(1.6)                               # 等待至少一轮 _stop.wait(1) → run_once
        assert worker._thread.is_alive()
    finally:
        worker.stop_memory_worker()
    assert not worker._thread.is_alive(), "stop 后线程必须在 join 期限内退出"
