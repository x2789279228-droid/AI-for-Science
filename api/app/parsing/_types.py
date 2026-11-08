"""解析层公共类型（避免 router ⇄ 引擎模块循环导入）。"""
from __future__ import annotations

from collections.abc import Callable

# 进度回调：(frac 0~1, message)
ProgressCb = Callable[[float, str], None]
