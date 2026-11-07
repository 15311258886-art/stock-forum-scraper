# -*- coding: utf-8 -*-
"""cohold 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    _named,
)

def api_cohold_recent(days: int = 5, min_holders: int = 2,
                      anchor: str | None = None, only_qualified: bool = False) -> dict:
    """近 N 日「多人同向操作」：共买 / 共卖 / 共持 三组，附时间、价格、数量、浮盈与持有状态。

    与 /api/consensus 的区别：那边看的是**当前持仓快照**（现在还有多少人拿着），
    这边看的是**最近 N 天谁同时动手了**（动作发生时点）。两者互补，不可互替。

    2026-10-10：新增独立 hold 组，把 HOLD 与 BUY 彻底分开（不再出现「持有混进买入」）。
    only_qualified=True 时只统计 Tier A 入围选手。

    名称补全在服务端统一做（与其它接口一致），避免前端各自回退出不一致的显示名。
    """
    d = DB.cohold_recent(days=days, min_holders=min_holders, anchor=anchor,
                         only_qualified=only_qualified)
    for grp in ("buy", "sell", "hold"):
        for x in d.get(grp) or []:
            n = _named(x["symbol"], x.get("symbol_name") or "")
            x["symbol_name"] = n["name"]
            x["company"] = n["company"]
            x["market"] = n["market"]
            x["name_missing"] = n["name_missing"]
            if n["name"]:
                x["label"] = f"{n['name']}（{x['symbol']}）"
            for p in x.get("players") or []:
                p["symbol_name"] = n["name"] or x["symbol_name"] or ""
    return d
