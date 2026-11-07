"""故障注入钩子：SCHOLAR_FAIL_AT="节点名[:次数]" 让指定节点前 N 次执行抛异常。

用途：测试断点续跑语义、比赛现场演示"崩溃 → 恢复"。节点在 graph.py 经 failable()
包装接入，不修改 nodes.py；环境变量未设置时为纯透传，零开销。
示例：SCHOLAR_FAIL_AT=verifier:1  →  verifier 第 1 次执行抛 RuntimeError，之后正常。
"""
from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_fail_counts: dict[str, int] = {}  # 进程内计数：同一节点抛过的异常次数


def _budgets() -> dict[str, int]:
    raw = (os.environ.get("SCHOLAR_FAIL_AT") or "").strip()
    out: dict[str, int] = {}
    for part in raw.split(","):
        if not part.strip():
            continue
        name, _, n = part.partition(":")
        if name.strip():
            out[name.strip()] = int(n) if n.strip().isdigit() else 1
    return out


def failable(name: str, fn):
    """包装节点函数：命中故障预算内时抛 RuntimeError（模拟硬崩溃）。"""

    def wrapper(state, config=None):
        budget = _budgets().get(name, 0)
        if budget > 0:
            with _lock:
                used = _fail_counts.get(name, 0)
                if used < budget:
                    _fail_counts[name] = used + 1
                    raise RuntimeError(f"[SCHOLAR_FAIL_AT] 注入故障：{name} 第 {used + 1} 次执行")
        return fn(state, config)

    return wrapper
