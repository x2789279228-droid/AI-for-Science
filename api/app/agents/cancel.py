"""用户停止生成：线程级协作取消注册表（POST /api/chat/{thread_id}/stop）。

单机单进程口径（与 ratelimit 一致）：内存 dict + threading.Event。responder 流式循环
逐 piece 检查，命中即中断生成并 guardrail=user_stopped 收尾（后续 verifier/claims 照常
校验已生成部分——与前端所见一致）。run/resume 收尾 pop 清理，dict 不泄漏。
"""
from __future__ import annotations

import threading

_flags: dict[str, threading.Event] = {}


def register(thread_id: str) -> threading.Event:
    ev = threading.Event()
    _flags[thread_id] = ev
    return ev


def cancel(thread_id: str) -> bool:
    ev = _flags.get(thread_id)
    if ev is None:
        return False
    ev.set()
    return True


def is_cancelled(thread_id: str) -> bool:
    ev = _flags.get(thread_id)
    return bool(ev and ev.is_set())


def pop(thread_id: str) -> None:
    _flags.pop(thread_id, None)
