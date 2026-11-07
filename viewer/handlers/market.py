# -*- coding: utf-8 -*-
"""market 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import time
from viewer.server import (  # noqa: E402,F401
    _MARKET_BENCHMARK,
    _MARKET_FRESH_TTL,
    _bench_em,
    _bench_sina,
    _bench_tencent,
    _is_trading_now,
    emapi,
    _MARKET_FRESH_CACHE,
)

def api_market_freshness() -> dict:
    """行情新鲜度：现抓基准标的（上证指数 sh000001）的**实时行情时间戳**，供顶栏如实显示。

    返回 {"market": {...}}：
      src          实际采纳的源（sina / tencent / eastmoney），谁先回用谁；
      symbol/name  基准标的（默认上证指数）；
      price        基准现值；
      quote_time   行情源自带的成交时间（秒级 YYYY-MM-DD HH:MM:SS）；
      server_now   服务端此刻时间；
      lag_seconds  server_now - quote_time（秒）。盘中正常 = 数秒~数十秒；
                   收盘后 = 自收盘到现在的时长（同时 is_trading=False，前端据此区分「延迟」与「休市」）；
      is_trading   当前是否盘中（工作日 + 9:30–11:30 / 13:00–15:00 + 权威交易日历确认）；
      ok           是否成功取到行情时间戳；失败时附 reason。

    设计：三源并发竞速（新浪 / 腾讯 / 东财），整体硬上限 3s；**一律现抓**，绝不用库里旧快照冒充；
    行情原子仅缓存 5s，且 lag/server_now/is_trading **每次现算**，不受缓存影响。
    """
    now_ts = time.time()
    atom = _MARKET_FRESH_CACHE.get("data")
    if atom is None or now_ts - float(_MARKET_FRESH_CACHE.get("ts") or 0.0) > _MARKET_FRESH_TTL:
        atom = emapi.race_first(
            [lambda: _bench_tencent(), lambda: _bench_sina(), lambda: _bench_em()],
            timeout=3.0)
        _MARKET_FRESH_CACHE["data"] = atom
        _MARKET_FRESH_CACHE["ts"] = now_ts
    market = {"symbol": _MARKET_BENCHMARK, "name": None, "price": None, "src": None,
              "quote_time": None, "server_now": time.strftime("%Y-%m-%d %H:%M:%S"),
              "lag_seconds": None, "is_trading": _is_trading_now(), "ok": False}
    if not atom or not atom.get("quote_time"):
        market["reason"] = "行情时间戳获取失败：新浪 / 腾讯 / 东财三源均不可达（本机网络或代理异常）"
        return {"market": market}
    qt = atom["quote_time"]
    try:
        lag = int(time.time() - time.mktime(time.strptime(qt, "%Y-%m-%d %H:%M:%S")))
        if lag < -60:          # 源时间快于本机时钟（时钟偏差）→ 不夸大延迟，按 0 计
            lag = 0
    except Exception:  # noqa: BLE001
        lag = None
    market.update({"name": atom.get("name"), "price": atom.get("price"), "src": atom.get("src"),
                   "quote_time": qt, "lag_seconds": lag, "ok": True})
    return {"market": market}
