"""解析层公共类型（避免 router ⇄ 引擎模块循环导入）。"""
from __future__ import annotations

from collections.abc import Callable

# 进度回调：(frac 0~1, message)
ProgressCb = Callable[[float, str], None]

# 协作式取消：引擎在 cancel_check 命中时抛出，jobs 层捕获后落 cancelled 终态
CancelCheck = Callable[[], bool]


class ParseCancelled(RuntimeError):
    """解析被用户取消（阶段边界/子进程终止）。区别于异常兜底：不触发 PyMuPDF 兜底。"""
