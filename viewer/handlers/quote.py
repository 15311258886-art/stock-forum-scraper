# -*- coding: utf-8 -*-
"""quote 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import time
from viewer.server import (  # noqa: E402,F401
    _quote_upstream,
)

def api_quote(symbol: str, force: bool = False) -> dict:
    """单只股票「重要信息」：行情 + 估值 + 财务（含报告期）。默认 60s 内存缓存(L1) + 5min 磁盘缓存(L2)。
    行情/估值：东财 push2，失败自动兜底腾讯行情；财务：东财 F10 具名字段。
    2026-10-23 性能修复：冷路径走「磁盘 TTL 缓存(5min) + 并发合并」，单只首开只打一次上游，
    5min 内重复开直接命中磁盘（东财调用=0），避免被限流卡死。"""
    if not symbol:
        return {"error": "symbol required"}
    key = (symbol or "").strip()
    if not force:
        hit = _QUOTE_CACHE.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
    if cu and not force:
        # L2 磁盘 5min：同 symbol 并发首开合并为一次上游调用（single-flight）。
        payload = cu.disk_cache(
            "quote", key, 300,
            lambda: cu.coalesce(f"quote:{key}",
                                lambda: _quote_upstream(key),
                                label="quote"))
    else:
        payload = _quote_upstream(key, force=force)
    if isinstance(payload, dict) and payload.get("error"):
        return payload
    _QUOTE_CACHE[key] = (time.time(), payload)
    return payload
