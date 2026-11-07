# -*- coding: utf-8 -*-
"""rate 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    screener,
)

def api_rate_snapshot(days: int = 30, limit: int = 30, rows: list = None,
                      src: str = "auto", trade_date: str = "") -> dict:
    """POST /api/rate/snapshot —— 把当前这版入手概率榜归档成当日快照（同日覆盖、幂等）。

    rows 由前端顺手带回（榜刚拉回来就存，秒回）；**不带 rows 才重跑全市场榜**（数分钟），
    那是给命令行/兜底用的慢路。归档的时间轴价值全在「存得勤」，故这条接口越轻越好。
    """
    if rows is None:
        d = screener.turnover_top(days=days, limit=limit)
        if not d.get("ok"):
            return d
        return dict(d, snapshot=screener.snapshot_top(d.get("rows") or [],
                                                      src=src,
                                                      trade_date=d.get("trade_date") or ""))
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    return {"ok": True, "snapshot": screener.snapshot_top(rows, src=src,
                                                          trade_date=trade_date or "")}
def api_rate_timeline(days_window: int = 10, per_day: int = 5) -> dict:
    """GET /api/rate/timeline —— 时间轴：各归档日的 T+1/3/5/10 收盘价与入市价比值。"""
    return screener.rate_timeline(days_window=days_window, per_day=per_day)
