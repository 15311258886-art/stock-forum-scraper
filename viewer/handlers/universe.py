# -*- coding: utf-8 -*-
"""universe 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_universe_scan(tier=None, limit=200):
    """P2：全宇宙多因子扫描结果（按排名降序）。tier=A/B/C/D 可筛选，limit 控制返回条数。"""
    import json as _json
    out = {"ok": True, "items": [], "tiers": {}}
    try:
        where = ""
        args = ()
        if tier:
            where = "WHERE tier=?"
            args = (tier,)
        rows = DB.conn.execute(
            "SELECT code,name,score,rank_pct,tier,momentum,volume,quality,reasons,updated "
            "FROM stock_universe_scan %s ORDER BY rank_pct DESC LIMIT ?" % where,
            args + (limit,)).fetchall()
        for code, name, score, rp, t, mom, vol, q, reasons, updated in rows:
            out["items"].append({"code": code, "name": name, "score": score,
                                 "rank_pct": rp, "tier": t, "momentum": mom,
                                 "volume": vol, "quality": q,
                                 "reasons": _json.loads(reasons or "[]"), "updated": updated})
        for t, n in DB.conn.execute(
                "SELECT tier, COUNT(*) FROM stock_universe_scan GROUP BY tier").fetchall():
            out["tiers"][t] = n
    except Exception as e:  # noqa: BLE001
        out["ok"] = False; out["error"] = str(e)
    return out
