"""loguru 结构化日志：接管标准库 logging（uvicorn/fastapi）+ 双 sink。

- 控制台：彩色简洁格式（开发/演示可读）
- 文件：data/logs/app.log，JSON 序列化，10MB×5 份轮转（评委可查结构化记录）
"""
from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path

from loguru import logger

from .config import DATA_DIR

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
    "<cyan>{extra[module]}</cyan> | <level>{message}</level>"
)


class InterceptHandler(logging.Handler):
    """把 std logging 记录重定向到 loguru（保留调用栈定位）。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage(), module=record.name)


def setup() -> None:
    """入口调用一次；重复调用安全（先清空自身挂过的 sink）。"""
    logger.remove()
    logger.configure(extra={"module": "app"})
    logger.add(sys.stderr, level="INFO", format=_CONSOLE_FORMAT,
               backtrace=False, diagnose=False)
    log_dir = Path(DATA_DIR) / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(str(log_dir / "app.log"), level="INFO", serialize=True,
                   rotation="10 MB", retention=5, enqueue=True,
                   backtrace=False, diagnose=False)
    except OSError:  # 只读盘等极端环境：控制台 sink 兜底
        logger.warning("log 文件目录不可写，仅保留控制台日志", module="log")

    # 接管 uvicorn / fastapi / 标准根 logger
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
        std = logging.getLogger(name)
        std.handlers = []
        std.propagate = True


__all__ = ["logger", "setup"]
