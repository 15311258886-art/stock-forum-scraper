# -*- coding: utf-8 -*-
"""player 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_player(name: str) -> dict:
    """实盘选手详情（富化）：渠道/名次/评分 + 持仓完整明细（买入时间/买入价/当前股价/权限）
    + 全部交易 + 近5日操作记录 + 受限项汇总。详情页核心接口，轻量（不含原始页面 HTML）。"""
    if not name:
        return {"error": "name required"}
    detail = DB.player_detail(name, recent_days=5)
    if detail is None:
        return {"error": "player not found"}
    return detail
