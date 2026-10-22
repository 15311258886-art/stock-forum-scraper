# -*- coding: utf-8 -*-
"""线程安全 LRU + TTL 缓存（P1-40：替代 20+ 全局裸 dict）。

原实现：`_TTL_CACHE` / `_KLINE_CACHE` / `_BOTTOM_CACHE` / `_ORG_CACHE` 等纯 dict，
grep 不到任何 pop/clear/maxsize 淘汰路径 —— 长期运行桌面进程内存单调增长 + check-then-fetch
竞态重复打外源。

本模块提供统一门面：
  · TTLCache(max_entries, ttl) —— 带上限 + TTL 的缓存，O(1) get/put，get 自动过期淘汰
  · get/put/invalidate/contains —— 等价原 dict 接口，便于渐进替换
  · 文件缓存（disk）留作后续：当前仅提供内存门面（最高 ROI 子集，P1-40）

注意：TTL 用 time.monotonic()（P2-07：纯墙钟在用户改系统时间时「永不过期」或「全部重抓惊群」）。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Hashable, Optional


class TTLCache:
    """容量受限 + TTL 的 LRU 缓存。线程安全。"""

    def __init__(self, max_entries: int = 512, ttl: float = 300.0):
        self.max_entries = max(1, int(max_entries))
        self.ttl = float(ttl)
        self._store: "OrderedDict[Hashable, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def _expired(self, deadline: float, now: float) -> bool:
        return now >= deadline

    def get(self, key: Hashable, default: Any = None) -> Any:
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return default
            deadline, val = item
            if self._expired(deadline, now):
                self._store.pop(key, None)
                return default
            # LRU：移到末尾
            self._store.move_to_end(key)
            return val

    def put(self, key: Hashable, val: Any, ttl: Optional[float] = None) -> None:
        now = time.monotonic()
        deadline = now + (self.ttl if ttl is None else float(ttl))
        with self._lock:
            self._store[key] = (deadline, val)
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)  # 淘汰最久未用

    def invalidate(self, prefix: Optional[str] = None) -> None:
        with self._lock:
            if prefix is None:
                self._store.clear()
            else:
                for k in [k for k in self._store if isinstance(k, str) and k.startswith(prefix)]:
                    self._store.pop(k, None)

    def contains(self, key: Hashable) -> bool:
        return self.get(key, _SENTINEL) is not _SENTINEL

    def __contains__(self, key: Hashable) -> bool:
        return self.contains(key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


_SENTINEL = object()


# 常用全局缓存实例（替代 server.py 的裸 dict，渐进替换用）
kline_cache = TTLCache(max_entries=1024, ttl=600)
bottom_cache = TTLCache(max_entries=256, ttl=1800)
org_cache = TTLCache(max_entries=512, ttl=3600)
api_cache = TTLCache(max_entries=256, ttl=300)


def get_cache(name: str) -> TTLCache:
    """按名取预置缓存实例。"""
    return {"kline": kline_cache, "bottom": bottom_cache,
            "org": org_cache, "api": api_cache}.get(name, api_cache)
