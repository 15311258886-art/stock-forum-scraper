"""viewer/cacheutil.py —— 磁盘 TTL 缓存 + 并发请求合并 + 全局东财令牌桶 + 限流抑制。

纯标准库实现，零业务依赖。用途：把弹窗各面板（K线 / 行情 / 资金流 / 新闻）的
「仅内存缓存」升级为「磁盘缓存兜底」，并合并并发同(symbol)请求、约束对东财的调用频率，
避免触发东财「连续~6次即限流」导致弹窗各分区长期"加载中"。

设计要点：
- disk_cache：命中 TTL 内直接返回；miss 才调 producer 并落盘；**只缓存成功响应，绝不缓存限流/空响应**。
  落盘文件 <cache_dir>/<ns>_<keyhash>.json（复用 server 侧 data/ 目录，与 F10 / lowbox 缓存同约定）。
- coalesce：同一 key 的并发请求合并为一次 producer 执行；其余请求 await 同一结果（降首开东财调用数）。
- TokenBucket：全局默认 4 次/秒，约束对东财的调用频率，跨股票也不超东财限流。
- 限流抑制：检测到东财 429/空体/限流特征（连续失败）时，短期跳过东财主源、直走新浪/腾讯兜底，
  不反复重试放大。
"""
from __future__ import annotations

import os
import json
import time
import threading
import hashlib
import logging

_log = logging.getLogger("cacheutil")

# 缓存目录：复用 server 侧 data/ 约定（与 F10 / lowbox 缓存同目录）
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

_lock = threading.Lock()
_store: dict = {}  # 内存索引 (namespace, key) -> (ts, payload)，避免每次读盘


def _path(namespace: str, key: str) -> str:
    h = hashlib.sha1(("%s|%s" % (namespace, key)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(_CACHE_DIR, "%s_%s.json" % (namespace, h))


def _is_cacheable(result) -> bool:
    """只缓存成功/有内容的响应；限流错误、空体、ok=False 一律不缓存。"""
    if result is None:
        return False
    if isinstance(result, dict) and ("error" in result or result.get("ok") is False):
        return False
    return True


def disk_cache(namespace: str, key: str, ttl_seconds: int, producer, is_cacheable=None):
    """磁盘 TTL 缓存（L2 兜底）：命中 TTL 内直接返回；miss 才调 producer 并落盘。
    返回 producer 结果（或 None）。失败/空响应不落盘。"""
    cache_file = _path(namespace, key)
    # 1) 内存索引（最快路径）
    with _lock:
        hit = _store.get((namespace, key))
    if hit is not None and (time.time() - hit[0]) < ttl_seconds:
        return hit[1]
    # 2) 磁盘
    try:
        if os.path.exists(cache_file):
            with open(cache_file, "r", encoding="utf-8") as fh:
                ent = json.load(fh)
            if isinstance(ent, dict) and "ts" in ent and "data" in ent:
                age = time.time() - float(ent["ts"])
                if age < ttl_seconds and _is_cacheable(ent["data"]):
                    with _lock:
                        _store[(namespace, key)] = (ent["ts"], ent["data"])
                    return ent["data"]
    except Exception:  # noqa: BLE001
        pass
    # 3) miss -> producer
    try:
        result = producer()
    except Exception:  # noqa: BLE001
        _log.exception("disk_cache producer 失败 ns=%s key=%s", namespace, key)
        return None
    if result is None or not _is_cacheable(result) or (is_cacheable is not None and not is_cacheable(result)):
        return result  # 失败/空响应不落盘
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = cache_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "data": result}, fh, ensure_ascii=False)
        os.replace(tmp, cache_file)
        with _lock:
            _store[(namespace, key)] = (time.time(), result)
    except Exception:  # noqa: BLE001
        _log.exception("disk_cache 落盘失败 ns=%s key=%s", namespace, key)
    return result


# ───────────────────────── 并发请求合并 ─────────────────────────
_coalesce: dict = {}
_coalesce_lock = threading.Lock()
_coalesce_runs = 0                 # 实际执行 producer 的累计次数（调试/探针用）
COALESCE_RUNS: dict = {}           # label -> 实际执行 producer 次数


def coalesce(key, producer, label=None):
    """同一 key 的并发请求合并为一次 producer 执行；其余请求 await 同一结果。
    返回 producer 结果（或重抛异常）。label 用于按端点统计实际执行次数。"""
    import concurrent.futures as _cf
    global _coalesce_runs
    with _coalesce_lock:
        fut = _coalesce.get(key)
        if fut is not None:
            wait = True
        else:
            fut = _coalesce[key] = _cf.Future()
            wait = False
    if wait:
        return fut.result()
    try:
        result = producer()
        _coalesce_runs += 1
        if label:
            COALESCE_RUNS[label] = COALESCE_RUNS.get(label, 0) + 1
        fut.set_result(result)
    except Exception as e:  # noqa: BLE001
        fut.set_exception(e)
    finally:
        with _coalesce_lock:
            _coalesce.pop(key, None)
    return fut.result()


def coalesce_runs_total():
    return _coalesce_runs


def coalesce_runs_by_label():
    return dict(COALESCE_RUNS)


# ───────────────────────── 全局东财令牌桶 ─────────────────────────
class TokenBucket:
    """简单令牌桶：默认 4 次/秒，跨股票也不超东财限流。"""

    def __init__(self, rate: float = 4.0, capacity=None):
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else max(1, int(rate)))
        self._tokens = float(self.capacity)
        self._ts = time.monotonic()
        self._lk = threading.Lock()

    def acquire(self, n: int = 1, block: bool = True, timeout: float = 30.0) -> bool:
        with self._lk:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._ts) * self.rate)
            self._ts = now
            if self._tokens >= n:
                self._tokens -= n
                return True
            if not block:
                return False
            deficit = n - self._tokens
            wait = deficit / self.rate
            if wait > timeout:
                # 等不起就直接放行（避免永久卡死），仍扣减到 0
                self._tokens = 0.0
                return True
        time.sleep(wait)
        with self._lk:
            self._tokens = 0.0
        return True


_em_bucket = TokenBucket(rate=4.0)


def eastmoney_acquire(block: bool = True, timeout: float = 30.0) -> bool:
    """在真正打东财前调用：受全局 4/s 令牌桶约束。"""
    return _em_bucket.acquire(1, block=block, timeout=timeout)


# ───────────────────────── 东财限流抑制 ─────────────────────────
_em_limited_until = 0.0
_em_fail_stamps: list = []      # 最近失败时间戳（滑动窗口）
_em_limited_lock = threading.Lock()
_EM_FAIL_WINDOW = 30.0
_EM_FAIL_THRESHOLD = 6
_EM_SUPPRESS_SECONDS = 60.0


def mark_eastmoney_failure() -> None:
    """记录一次东财失败；30s 窗口内累计达阈值则进入抑制（短期跳过东财主源）。"""
    global _em_limited_until
    now = time.time()
    with _em_limited_lock:
        _em_fail_stamps.append(now)
        # 仅保留窗口内的失败时间戳
        _em_fail_stamps = [t for t in _em_fail_stamps if now - t <= _EM_FAIL_WINDOW]
        if len(_em_fail_stamps) >= _EM_FAIL_THRESHOLD:
            _em_limited_until = now + _EM_SUPPRESS_SECONDS
            _em_fail_stamps.clear()


def mark_eastmoney_success() -> None:
    """成功则平缓回退失败计数（窗口内时间戳自然过期即可，这里只做轻量衰减）。"""
    with _em_limited_lock:
        if _em_fail_stamps:
            _em_fail_stamps.pop(0)


def eastmoney_suppressed() -> bool:
    return time.time() < _em_limited_until


def reset_eastmoney_state() -> None:
    """测试/探针用：清空限流抑制与失败计数。"""
    global _em_limited_until, _coalesce_runs
    _em_limited_until = 0.0
    _em_fail_stamps.clear()
    _coalesce_runs = 0
    COALESCE_RUNS.clear()
    with _coalesce_lock:
        _coalesce.clear()
    with _lock:
        _store.clear()
