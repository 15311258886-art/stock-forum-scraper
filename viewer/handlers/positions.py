# -*- coding: utf-8 -*-
"""positions 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_positions() -> list[dict]:
    rows = DB.conn.execute(
        "SELECT site, player, channel, symbol, symbol_name, ratio, action, post_url "
        "FROM positions WHERE symbol IS NOT NULL ORDER BY symbol, player"
    ).fetchall()
    return [dict(r) for r in rows]
