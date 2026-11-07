# -*- coding: utf-8 -*-
"""person 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import datetime
from viewer.server import (  # noqa: E402,F401
    DB,
    _hold_days,
)

def api_person(name: str) -> dict:
    """按姓名/昵称聚合该用户的信息（不加载任何原始页面 HTML，轻量快速）。"""
    if not name:
        return {"error": "name required"}
    row = DB.conn.execute("SELECT * FROM persons WHERE name=?", (name,)).fetchone()
    meta = dict(row) if row else {"name": name, "mention_count": 0, "role": "unknown"}
    posts = [dict(r) for r in DB.conn.execute(
        "SELECT id, site, post_time, text, sentiment FROM posts WHERE author=? ORDER BY post_time DESC LIMIT 50",
        (name,)).fetchall()]
    trades = [dict(r) for r in DB.conn.execute(
        "SELECT id, site, post_time, action, symbol, symbol_name, price, quantity, "
        "buy_time, sell_time, hold_since FROM trades WHERE author=? ORDER BY post_time DESC LIMIT 50",
        (name,)).fetchall()]
    positions = [dict(r) for r in DB.conn.execute(
        "SELECT channel, site, symbol, symbol_name, ratio, action, post_url, post_time, "
        "buy_time, buy_price, current_price, sell_time, sell_price, hold_since "
        "FROM positions WHERE player=? ORDER BY symbol",
        (name,)).fetchall()]
    # 2026-09-23 修复（一致性收尾）：标注每只标的的持仓起始日期与持有天数，并显式声明基准。
    # 原实现按「每条记录自身的 buy_time」逐行算 hold_days，既没标口径也没给 trades 赋值。
    # 现在与 /api/player(db.py player_detail 的 since_map / _enrich，473-530 行) 完全同口径：
    # 建仓起点只取自**该人自身**的 positions+trades，绝不取全市场最早（那是 /api/stock）。
    def _act(it) -> str:
        return (it.get("action") or "").strip().upper()

    records = positions + trades
    inferred: dict[str, str] = {}
    explicit_since: dict[str, str] = {}
    for it in records:
        sym = (it.get("symbol") or "").strip()
        if not sym:
            continue
        v = (it.get("hold_since") or "").strip()
        if v:
            explicit_since.setdefault(sym, v)
        if _act(it) in ("BUY", "HOLD"):
            ts = (it.get("buy_time") or it.get("post_time") or "").strip()
            if ts and (sym not in inferred or ts < inferred[sym]):
                inferred[sym] = ts
    since_map = {**inferred, **explicit_since}
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    for it in records:
        since = since_map.get((it.get("symbol") or "").strip()) or ""
        it["hold_since"] = since
        is_sell = _act(it) == "SELL"
        # 与 db.py:511-520 同口径：卖出记录算到卖出日（区间长度），在持记录算到今日（已持有）
        end = ((it.get("sell_time") or it.get("buy_time") or it.get("post_time") or "").strip()
               if is_sell else today)
        it["hold_days"] = _hold_days(since, end)
        # 与 db.py:529 同口径标注：closed=已了结 / open=仍在持（决定 hold_days 的终点）
        it["hold_days_mode"] = "closed" if is_sell else "open"
    # 顶层「本人首次建仓」口径的总天数：取该人自身最早的一笔建仓至今
    own_since = min((v for v in since_map.values() if v), default="")
    hold_days = _hold_days(own_since) if own_since else None
    for p in positions:
        # 2026-09-22 修复 P2-4：此前未返回 channel，前端 app.js 只能兜底成 'A'，
        # 导致人物抽屉的渠道徽章恒显示「A 实盘大赛」。这里如实带回（缺失时给空串，不留兜底）。
        p["channel"] = (p.get("channel") or "")
    # 提及过的股票（去重）
    mentioned = []
    seen = set()
    for p in posts:
        for code in ("600519", "300750", "000858", "002594", "601318", "000725", "603259",
                     "300059", "600036", "002230", "000333", "603501"):
            if code in (p.get("text") or "") and code not in seen:
                seen.add(code); mentioned.append(code)
    return {
        "name": name,
        # 2026-09-23 修复（一致性收尾，A 点）：显式声明本视图 hold_days 的基准口径。
        # person_first_build = 建仓起点取自「该人自身」最早的一笔建仓记录（positions+trades，
        # 与 /api/player 的 player_first_build 同为「本人」口径，故两处天数可对得上）；
        # 与 /api/stock 的 symbol_first_build（全市场最早建仓）不同，故个股页天数可能更大。
        "hold_days_basis": "person_first_build",
        "hold_days": hold_days,
        "meta": meta, "posts": posts, "trades": trades,
        "positions": positions, "mentioned_stocks": mentioned,
        "post_count": len(posts), "trade_count": len(trades),
        "position_count": len(positions),
    }
