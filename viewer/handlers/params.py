# -*- coding: utf-8 -*-
"""params 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging, sqlite3
from viewer.server import (  # noqa: E402,F401
    EXECUTABILITY,
    KPARAMS,
    UNITS,
    _APP_DB,
)

def api_params() -> dict:
    """/api/params —— 参数注册表自述 + 执行可行性假设清单。

    为什么要把"参数"做成一个接口：测评指出同一套代码里 40 与 500 两个阈值
    共存一个多月、两次评审都没发现。把参数和它的**矛盾带宽**一起暴露出来，
    是让"不一致"变成可以看见的东西，而不是靠人记得住。
    """
    try:
        out = {"ok": True, "params": KPARAMS.describe(),
               "executability": EXECUTABILITY.describe()}
    except Exception as e:  # noqa: BLE001
        logging.exception("api_params 失败")
        return {"ok": False, "error": str(e)[:200]}
    try:
        con = sqlite3.connect(_APP_DB, timeout=15)
        try:
            out["volume_units"] = UNITS.snapshot_volume_unit_stats(con)
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        out["volume_units"] = {"error": "unavailable"}
    return out
