# -*- coding: utf-8 -*-
"""operating 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    _player_index,
)

def api_operating() -> list[dict]:
    # 2026-09-24：多返回 post_url 和 post_id，前端据此识别 demo（demo_data 写入的 post_url 全是 example.local
    # 且 post_id 以 'demo-' 开头）。不返回 post_url 的话前端没法分辨 demo vs 真实。
    rows = DB.conn.execute(
        "SELECT id, site, author, post_id, post_url, post_time, action, symbol, symbol_name, "
        "price, quantity, raw_text "
        "FROM trades WHERE action IN ('BUY','SELL','HOLD') ORDER BY post_time DESC LIMIT 200"
    ).fetchall()
    idx = _player_index()
    out = []
    for r in rows:
        d = dict(r)
        meta = idx.get(d.get("author") or "", {})
        d["author_tier"] = meta.get("tier")
        d["author_platform"] = meta.get("platform") or d.get("site")
        out.append(d)
    return out
