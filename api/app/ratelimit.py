"""进程内令牌桶限流（Round4 横切生产化）：零新依赖。

口径（如实注明）：单机单进程、匿名多用户画像——按 X-User-Id（经 recommend 路由
同款校验的匿名 id）优先、否则 client host 分桶；重端点（LLM/IO 密集 POST）默认
30 次/分/用户，env RATE_LIMIT_PER_MIN 可调（0=关闭）。不是分布式限流，也不是
多租户鉴权——比赛演示档的滥用防护。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict


class _Bucket:
    __slots__ = ("tokens", "updated")

    def __init__(self, capacity: int) -> None:
        self.tokens = float(capacity)   # 首屏即满桶：允许瞬时 burst ≤ capacity
        self.updated = time.monotonic()


class RateLimiter:
    """按 key 的令牌桶池（懒建 + 容量上限淘汰，防 key 无限增长）。"""

    _MAX_KEYS = 4096

    def __init__(self, rate_per_min: int) -> None:
        self.rate = max(0, int(rate_per_min))
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        """取一枚令牌。返回 (是否放行, 建议等待秒数)。rate=0 恒放行。"""
        if self.rate <= 0:
            return True, 0.0
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                if len(self._buckets) >= self._MAX_KEYS:
                    self._buckets.popitem(last=False)   # FIFO 淘汰最旧 key
                b = self._buckets[key] = _Bucket(self.rate)
            else:
                self._buckets.move_to_end(key)          # LRU touch
            now = time.monotonic()
            b.tokens = min(float(self.rate),
                           b.tokens + (now - b.updated) * self.rate / 60.0)
            b.updated = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True, 0.0
            retry = (1.0 - b.tokens) * 60.0 / self.rate
            return False, retry

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
