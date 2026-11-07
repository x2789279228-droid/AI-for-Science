"""服务重启时的会话自动恢复：扫描未完成线程，后台顺序续跑。

- 崩溃遗留的 running（进程死亡时中断）与已 failed 的线程均在扫描范围，
  每线程恢复尝试次数受 threads_store.MAX_RESUME_ATTEMPTS 上限约束
- 恢复过程 SSE 事件入丢弃队列：结果落 checkpoints.db 与 chat_threads，
  用户回到会话页经 GET /api/threads 可见最终答案
- 由 main.py lifespan 在后台守护线程启动，不阻塞服务就绪
"""
from __future__ import annotations

import logging
import queue as queue_mod
import threading

from .. import threads_store
from . import runner

log = logging.getLogger("recovery")


def start_recovery_worker() -> int:
    """扫描并启动恢复；返回本次派发的恢复线程数（便于日志/测试断言）。"""
    threads_store.init()
    rows = threads_store.pending_recovery()
    if not rows:
        return 0
    t = threading.Thread(target=_resume_all, args=(rows,), daemon=True,
                         name="checkpoint-recovery")
    t.start()
    return len(rows)


def _resume_all(rows: list[dict]) -> None:
    for row in rows:
        tid = row["thread_id"]
        log.info("启动恢复：续跑线程 %s（turn %s，此前 %s 次）",
                 tid, row["turn"], row["resume_attempts"])
        try:
            runner.resume_stream(tid, queue_mod.Queue())
        except Exception as e:  # noqa: BLE001 —— 单线程恢复失败不影响其余
            log.warning("自动恢复失败 %s: %s", tid, e)
