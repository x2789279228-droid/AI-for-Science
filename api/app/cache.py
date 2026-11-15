"""进程内缓存基建：线程安全 TTLCache + 全局数据版本（data_revision）。

单机 SQLite 项目不需要 Redis——热点只读载荷（文献列表/族视图/图谱/DocIR）
用进程内 TTL 缓存消除重复计算；写路径（上传/解析完成/删除/改名/去重决策）
bump data_revision，所有以 revision 为键的缓存自然失效。

设计约束：
- TTLCache 线程安全（get/set 加锁），供 FastAPI 请求线程与解析 worker 并发使用
- revision 是模块级 int（进程内语义）：重启即缓存全空，无需持久化
- 命中信息随载荷返回（cached/cache_age），答辩可展示「二次加载命中缓存」
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from typing import Any


class TTLCache:
    """TTL + 容量上限的线程安全缓存（get 不续期，set 刷新时间戳）。

    value_factory 模式：get_or_build 原子化「查→算→存」，并发同键请求
    可能重复计算（不排队），但结果一致且写回唯一——对只读缓存足够。
    实例自注册到 _ALL_CACHES（clear_all 供测试隔离/运维一键清空）。
    """

    def __init__(self, maxsize: int = 64, ttl: float = 60.0) -> None:
        self._maxsize = max(int(maxsize), 1)
        self._ttl = float(ttl)
        self._lock = threading.Lock()
        self._data: dict[Hashable, tuple[float, Any]] = {}
        with _registry_lock:
            _ALL_CACHES.append(self)

    def get(self, key: Hashable) -> tuple[Any, float] | None:
        """命中返回 (value, age_s)，未命中/过期返回 None（过期条目顺手清除）。"""
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            ts, value = item
            age = now - ts
            if age >= self._ttl:
                self._data.pop(key, None)
                return None
            return value, age

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            if len(self._data) >= self._maxsize and key not in self._data:
                # 淘汰最旧一条（时间戳最小）；容量是护栏不是 LRU 精确语义
                oldest = min(self._data, key=lambda k: self._data[k][0])
                self._data.pop(oldest, None)
            self._data[key] = (time.monotonic(), value)

    def get_or_build(self, key: Hashable, build: Callable[[], Any]) -> tuple[Any, float, bool]:
        """返回 (value, age_s, cached)；未命中时执行 build 并写回。"""
        hit = self.get(key)
        if hit is not None:
            return hit[0], hit[1], True
        value = build()
        self.set(key, value)
        return value, 0.0, False

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_registry_lock = threading.Lock()
_ALL_CACHES: list[TTLCache] = []


def clear_all() -> None:
    """清空全部注册缓存（测试隔离 autouse fixture / 运维热刷新用）。"""
    with _registry_lock:
        caches = list(_ALL_CACHES)
    for c in caches:
        c.clear()


# ---- 全局数据版本（写路径 bump → 键含 revision 的缓存整体失效） ----

_revision_lock = threading.Lock()
_revision = 0


def get_revision() -> int:
    return _revision


def bump_revision() -> int:
    """数据变更后调用：返回新版本号（只增）。"""
    global _revision
    with _revision_lock:
        _revision += 1
        return _revision


def rev_key(*parts: Hashable) -> tuple:
    """把 revision 并入缓存键的便捷封装。"""
    return (get_revision(), *parts)
