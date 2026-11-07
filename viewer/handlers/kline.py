# -*- coding: utf-8 -*-
"""kline 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import time
from viewer.server import (  # noqa: E402,F401
    _kline_from_local_db,
    _kline_upstream,
    _normalize_kline_volume,
    _sina_kline_result,
    _slice_kline,
    _tencent_kline_result,
    emapi,
    _KLINE_CACHE,
    cu,
)

def api_kline(symbol: str, period: str = "day", limit: int = 250, offset: int = 0) -> dict:
    """单只股票 K 线：东财（前复权·全历史）→ 新浪（不复权·全历史）→ 腾讯（前复权·兜底）。
    三源全部不可达时，回退本地全市场日 K 库 data/kline_daily.db（见 _kline_from_local_db）。
    返回 OHLCV + 最新价/涨跌幅 + 首末日期（供前端判断能否拖到上市首日）。

    **默认只回最近 250 根**（limit 默认 250）：几千根全历史推过去既慢又没人看；
    前端要「拖到上市首日 / 点全历史 / 缩到底」时传 limit=0 拉全量，缓存按 (symbol, period) 复用。

    2026-10-23 性能修复：冷路径走「磁盘 TTL 缓存(1h) + 并发合并(single-flight)」——
    首开只打一次上游，1h 内重复开直接命中磁盘（东财调用=0），避免被限流卡死。

    2026-10-13（进哥）：limit=0 要的是「上市至今」**真·全历史**。若竞速只拿到腾讯单页兜底
    （~120 根，full_history=False），必须改用「新浪全量 → 腾讯分段」认真拉全历史重试，
    绝不允许把半年走势当成上市行情（此前误判、直接影响判断的根源）。东财/新浪本就返回全历史，
    直接打 full_history=True 标记。
    """
    if not symbol:
        return {"error": "symbol required"}
    period = period if period in ("day", "week", "month") else "day"
    key = (symbol, period)
    now = time.time()
    cached = _KLINE_CACHE.get(key)
    if cached and now - cached[0] < 90:
        cp = cached[1]
        # 要全历史但缓存里只有单页兜底 → 不命中内存缓存，走下面的「认真拉全历史」重试
        if limit == 0 and not cp.get("full_history"):
            pass
        else:
            return _slice_kline(cp, limit, offset)
    # 冷路径：磁盘 1h TTL 缓存（L2） + 并发合并（同 (symbol,period) 只打一次上游，L1 内存 90s 之上）。
    if cu:
        payload = cu.disk_cache(
            "kline", f"{symbol}|{period}", 3600,
            lambda: cu.coalesce(f"kline:{symbol}:{period}",
                                lambda: _kline_upstream(symbol, period),
                                label="kline"))
    else:
        payload = _kline_upstream(symbol, period)
    if payload is None:
        # 2026-10-28（进哥）：联网三源全挂时回退本地库，K 线图照样能看（非实时）
        local = _kline_from_local_db(symbol, period)
        if local is not None:
            payload = local
        else:
            return {"error": "行情获取失败：代码可能无效，且本地网络无法访问行情源"
                             "（东方财富 / 新浪 / 腾讯均不可用）；本地 K 线库也未收录该代码。",
                    "symbol": symbol, "local_unavailable": True}
    # limit=0（要上市至今）但竞速只拿到腾讯单页兜底 → 认真拉全历史重试：新浪全量优先，
    # 腾讯分段（full_history=True）兜底。二者任一中即可拿到真·全历史。
    if limit == 0 and not payload.get("full_history"):
        full = emapi.race_priority(
            lambda: _sina_kline_result(symbol, period, timeout=10, retries=2),
            [lambda: _tencent_kline_result(symbol, period, timeout=10, retries=2,
                                           full_history=True)],
            grace=3.0, timeout=12.0)
        if full is not None:
            payload = full
            payload["full_history"] = True
    payload.setdefault("adjust", "qfq")
    if payload.get("klines"):
        payload.setdefault("first_date", payload["klines"][0].get("date", ""))
        payload.setdefault("last_date", payload["klines"][-1].get("date", ""))
    # 2026-10-31：磁盘 L2 缓存（1h TTL）里可能躺着修复前写入、没有单位声明的 payload，
    # 直接下发等于把「猜单位」的坑留着。这里补一次归一（幂等，已有单位则只补顶层字段）。
    if payload.get("klines") and "volume_unit" not in payload:
        _normalize_kline_volume(payload)
    _KLINE_CACHE[key] = (now, payload)
    return _slice_kline(payload, limit, offset)
