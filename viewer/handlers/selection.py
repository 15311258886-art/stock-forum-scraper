# -*- coding: utf-8 -*-
"""selection 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging
from viewer.server import (  # noqa: E402,F401
    DB,
    SELECTION,
)

def api_selection() -> dict:
    """选股方向（2026-10-30）：短线形态证伪 + 低波池。

    只读落表结果。池子日更（7 秒），证据回测月度（143 秒）。
    """
    try:
        return SELECTION.payload(DB.conn)
    except Exception:  # noqa: BLE001
        logging.exception("api_selection 失败")
        return {"ok": False, "available": False, "error": "选股方向数据不可用"}
