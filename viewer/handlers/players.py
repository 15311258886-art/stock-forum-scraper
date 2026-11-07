# -*- coding: utf-8 -*-
"""players 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_players(top_only: bool = False, channel: str | None = None,
                tier: str | None = None, visible_only: bool = False) -> list[dict]:
    """实盘选手列表。

    - top_only：仅入选核心池
    - channel：按来源渠道(A/B/C/D)过滤
    - tier：按层级(A~F)过滤；需求要求 E/F 低层级**一律露出**，所以默认不排除
      is_visible=0 的选手（当前写入时 is_visible 恒为 1）。
    - visible_only：是否只保留 is_visible=1（默认 False，展示全部层级）。
    """
    sql = "SELECT * FROM players"
    where, params = [], []
    if top_only:
        where.append("is_top_player=1")
    if channel:
        where.append("channel=?")
        params.append(channel)
    if tier:
        where.append("tier=?")
        params.append(tier)
    if visible_only:
        where.append("is_visible=1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY rank_position ASC"
    players = [dict(r) for r in DB.conn.execute(sql, params).fetchall()]

    # ---------- 当前持仓（2026-09-25 王总需求：实盘选手列表直接显示各自持有哪些票） ----------
    # 口径：positions 显式持仓优先，trades 操作流水兜底；按 (选手, 标的) 取最近一次动作。
    # 匹配键=选手昵称（与 player_detail 同口径）。抓取不到持仓的选手 holdings 为空，前端留白。
    # 2026-09-25 修正：仅 symbol 缺失不再整批丢弃——有股票名也可显示（标 name_only=待匹配代码）；
    #   来源 source_type 区分 bar_post(个股讨论页提及) / player_page(选手自己主页实盘记录)。
    sym_names: dict[str, str] = {}
    hold_map: dict[str, dict[tuple, dict]] = {}

    # 2026-09-25 修复：原实现对 sqlite3.Row 调用 r.get("source_type")，
    #   但 sqlite3.Row 没有 .get() 方法 → 直接抛 AttributeError，使 /api/players 整体 500。
    #   且 positions 表本就没有 source_type 列（只有 site），故改为「有该列则用，无则由 site 推导」。
    #   推导口径：淘股吧=选手主页实盘记录(player_page)；集思录/摩尔=组合晒单(community)；
    #            其余（雪球/东财/同花顺等讨论页）= bar_post(个股讨论提及)。
    _SITE_SRC = {"taoguba": "player_page", "tgb": "player_page",
                 "jisilu": "community", "moer": "community"}

    def _src_of(r) -> str:
        keys = r.keys() if hasattr(r, "keys") else []
        if "source_type" in keys:
            v = r["source_type"]
            if v:
                return str(v)
        site = (r["site"] if "site" in keys else "") or ""
        return _SITE_SRC.get(str(site).lower(), "bar_post")

    def _feed(rows) -> None:
        for r in rows:
            name = r["name"]
            sym = r["symbol"]
            sname = r["symbol_name"] or ""
            stype = _src_of(r)
            if not sym and not sname:
                continue
            if sym and sname:
                sym_names.setdefault(sym, sname)
            cur = hold_map.setdefault(name, {})
            key = (name, sym or sname)
            prev = cur.get(key)
            rec = {"symbol": sym or "", "symbol_name": sname,
                   "action": r["action"] or "", "post_time": r["post_time"],
                   "source_type": stype, "name_only": not bool(sym)}
            if prev is None or str(r["post_time"] or "") > str(prev["post_time"] or ""):
                cur[key] = rec

    _feed(DB.conn.execute(
        "SELECT player AS name, symbol, symbol_name, action, post_time, site FROM positions").fetchall())
    # 2026-09-25 修复：trades 表同样没有 source_type 列（PRAGMA 已核实），一并去掉，由 site 推导。
    _feed(DB.conn.execute(
        "SELECT author AS name, symbol, symbol_name, action, post_time, site FROM trades").fetchall())

    for p in players:
        items = list(hold_map.get(p["name"], {}).values())
        items.sort(key=lambda x: str(x["post_time"] or ""), reverse=True)
        for it in items:
            it["display"] = it["symbol_name"] or it["symbol"]
        p["holdings"] = items
    return players
