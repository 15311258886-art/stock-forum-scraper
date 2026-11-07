# -*- coding: utf-8 -*-
"""indicators 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_indicators_signals(direction="buy", limit=60):
    """P1：因子买卖信号候选（最新一日）。dir=buy/sell，按 score 降序。"""
    import json as _json
    out = {"ok": True, "dir": direction, "items": []}
    try:
        if direction == "sell":
            rows = DB.conn.execute(
                "SELECT code,name,date,sell_score,sell_reasons FROM stock_signals "
                "WHERE sell_score>0 ORDER BY sell_score DESC LIMIT ?", (limit,)).fetchall()
            key = "sell_reasons"
        else:
            rows = DB.conn.execute(
                "SELECT code,name,date,buy_score,buy_reasons FROM stock_signals "
                "WHERE buy_score>0 ORDER BY buy_score DESC LIMIT ?", (limit,)).fetchall()
            key = "buy_reasons"
        for code, name, date, score, reasons in rows:
            out["items"].append({"code": code, "name": name, "date": date,
                                 "score": score, "reasons": _json.loads(reasons or "[]")})
    except Exception as e:  # noqa: BLE001
        out["ok"] = False; out["error"] = str(e)
    return out
def api_indicators(code=None):
    """P1：单只票最新因子快照。"""
    if not code:
        return {"ok": False, "error": "code required"}
    row = DB.conn.execute(
        "SELECT * FROM stock_indicators WHERE code=?", (code,)).fetchone()
    if not row:
        return {"ok": False, "error": "no indicators for %s" % code}
    cols = [d[0] for d in DB.conn.execute(
        "PRAGMA table_info(stock_indicators)").fetchall()]
    return {"ok": True, "indicators": dict(zip(cols, row))}
