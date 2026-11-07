# -*- coding: utf-8 -*-
"""harvest 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging
from viewer.server import (  # noqa: E402,F401
    DB,
    HARVEST,
)

def api_harvest() -> dict:
    """收割视角（2026-10-31）：局面状态 + 今日陷阱名单 + 实测证据。

    数据来自 data/kline_daily.db 的完整日线（1,700 万行），由 regime_daily 任务
    每日收盘后增量写入 market_structure_daily / harvest_trap_daily。
    本接口只读，不现算（避免请求线程扫日线库）。
    """
    try:
        return HARVEST.payload(DB.conn)
    except Exception:  # noqa: BLE001
        logging.exception("api_harvest 失败")
        return {"ok": False, "error": "局面数据不可用"}
