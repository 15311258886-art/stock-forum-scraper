# -*- coding: utf-8 -*-
"""consensus 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    _named,
)

def api_consensus(min_holders: int = 2, symbol: str | None = None,
                  only_qualified: bool = False,
                  max_age_days: int | None = None) -> list[dict]:
    """多人共同持仓共识：各股被多少人（跨渠道/平台）共同持有，按人数降序，
    融合讨论热度作为辅助参考。symbol 指定时只返回单只，避免每次点击重算全量。

    2026-10-10 口径改革：返回结构把「喊买入(BUY)」「喊持有(HOLD)」「持仓快照(WATCH)」
    分开计数 —— buy_holders / hold_holders / watch_holders，holders 为三者去重总数。
    only_qualified=True 时只统计 Tier A 入围选手（players.tier='A'）。

    2026-10-15 P0-1：透传 max_age_days（positions 时效闸，默认 90 天）；每行已携带
    data_asof / positions_max_age_days，前端取首行即可渲染「数据截至 YYYY-MM-DD」角标。"""
    out = DB.consensus_stocks(min_holders=min_holders, only_qualified=only_qualified,
                              max_age_days=max_age_days)
    for x in out:
        n = _named(x["symbol"], x.get("symbol_name") or "")
        x["symbol_name"] = n["name"]
        x["company"] = n["company"]
        x["market"] = n["market"]
        x["name_missing"] = n["name_missing"]
    if symbol:
        out = [x for x in out if x["symbol"] == symbol]
    return out
