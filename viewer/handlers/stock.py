# -*- coding: utf-8 -*-
"""stock 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    _hold_days,
    _named,
    screener,
)

def api_stock_turnover(symbol: str, days: int = 30) -> dict:
    """GET /api/stock_turnover?symbol=XXXXXX —— 单票换手率实战评分（个股详情抽屉，2026-09-30）。"""
    return screener.turnover_single(symbol, days=days)
def api_stock_f10(symbol: str) -> dict:
    """GET /api/stock_f10?symbol=XXXXXX —— 个股 F10：公司简介 + 持股结构（机构/散户）+ 股东户数（2026-09-30 晚）。"""
    return screener.stock_f10(symbol)
def api_stock(symbol: str) -> dict:
    """单只股票明细：讨论帖 + 各选手持仓（含买入时间/买入价/当前股价/权限）+ 相关实盘交易。
    轻量接口（不含原始页面 HTML），供「当前讨论股票」卡片点击查看。"""
    if not symbol:
        return {"error": "symbol required"}
    mentions = [dict(r) for r in DB.conn.execute(
        "SELECT sm.context, p.site, p.author, p.post_time, p.sentiment, p.text "
        "FROM stock_mentions sm JOIN posts p ON p.id = sm.post_id_ref "
        "WHERE sm.symbol=? ORDER BY p.post_time DESC LIMIT 50", (symbol,)).fetchall()]
    trades = [dict(r) for r in DB.conn.execute(
        "SELECT id, site, author, post_time, action, symbol, symbol_name, price, quantity, "
        "buy_time, buy_price, current_price, sell_time, sell_price, hold_since, "
        "access_level, access_hint FROM trades "
        "WHERE symbol=? ORDER BY COALESCE(buy_time, post_time) DESC LIMIT 100", (symbol,)).fetchall()]
    positions = [dict(r) for r in DB.conn.execute(
        "SELECT player, channel, site, symbol, symbol_name, ratio, action, post_time, post_url, "
        "buy_time, buy_price, current_price, sell_time, sell_price, hold_since, "
        "access_level, access_hint FROM positions "
        "WHERE symbol=? ORDER BY ratio DESC", (symbol,)).fetchall()]

    # 持仓起始日期：该股最早的建仓时间（显式披露优先），并把「持有天数」一并算出，
    # 便于在「持仓选手」表中直接看到"各人从何时开始持有该股"。
    since = ""
    for it in positions + trades:
        v = (it.get("hold_since") or "").strip()
        if v and (not since or v < since):
            since = v
    if not since:
        for it in positions + trades:
            if (it.get("action") or "").upper() not in ("BUY", "HOLD"):
                continue
            v = (it.get("buy_time") or it.get("post_time") or "").strip()
            if v and (not since or v < since):
                since = v
    # 2026-09-22 修复 P2-3：原实现把「该条记录自身的 buy_time / sell_time」当作结束点，
    # 于是同一只股票各条记录算出 0/2/52/59… 等互不相同的天数，与旁边同一行的
    # 「持仓起始日期」(since) 自相矛盾（实测 600519 共 12 条、10 条显示"已持有 0 天"）。
    # 改为统一以「该标的持仓起始日 → 今日」计算，同一 symbol 的所有记录天数一致。
    hold_days = _hold_days(since)
    for it in positions + trades:
        it["hold_since"] = since
        it["hold_days"] = hold_days
        if (it.get("action") or "").upper() == "SELL":
            if not (it.get("sell_time") or "").strip():
                it["sell_time"] = it.get("post_time") or it.get("buy_time") or ""
            if it.get("sell_price") is None:
                it["sell_price"] = it.get("buy_price") if it.get("buy_price") is not None else it.get("price")
    name = next((r["symbol_name"] for r in trades if r.get("symbol_name")), "")
    if not name:
        row = DB.conn.execute(
            "SELECT symbol_name FROM positions WHERE symbol=? AND symbol_name IS NOT NULL LIMIT 1",
            (symbol,)).fetchone()
        name = (row["symbol_name"] if row else "") or ""
    named = _named(symbol, name)
    holders = {p["player"] for p in positions if p.get("player")}
    # 「多空观点明细」：把对该股的讨论帖按立场分组，逐条保留「谁 + 原话理由」
    views = {"bull": [], "bear": [], "neutral": []}
    for m in mentions:
        cat = {"看多": "bull", "看空": "bear"}.get(m.get("sentiment"), "neutral")
        views[cat].append({
            "author": m.get("author") or "匿名",
            "post_time": m.get("post_time") or "",
            "site": m.get("site") or "",
            "text": m.get("text") or m.get("context") or "",
        })
    view_summary = {k: len(v) for k, v in views.items()}
    return {"symbol": symbol, "symbol_name": named["name"], "company": named["company"],
            "market": named["market"], "name_missing": named["name_missing"],
            # 2026-09-22 修复（A 点）：显式声明本视图 hold_days 的基准口径——
            # symbol_first_build = 建仓起点取自「该标的全市场」最早建仓记录，
            # 所有记录共用一个 hold_days（与 /api/player 的 player_first_build 不同）。
            "hold_days_basis": "symbol_first_build",
            "hold_days": hold_days,
            "mentions": mentions, "views": views, "view_summary": view_summary,
            "trades": trades,
            "positions": positions, "holders": len(holders), "mention_count": len(mentions),
            "trade_count": len(trades), "position_count": len(positions)}
