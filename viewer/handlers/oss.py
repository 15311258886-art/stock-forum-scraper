# -*- coding: utf-8 -*-
"""oss 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging
from viewer.server import (  # noqa: E402,F401
    DB,
    OSS,
)

def api_oss() -> dict:
    """开源情报（2026-10-30）：GitHub 上的量化规则在本系统数据上重测的结果。

    只读落表结果，不现算 —— 回测在 oss_daily 的离进程任务里跑。
    返回里必带 data_span：主结果样本只有约 14 个月，不披露就是误导。
    """
    try:
        return OSS.payload(DB.conn)
    except Exception:  # noqa: BLE001
        logging.exception("api_oss 失败")
        return {"ok": False, "available": False, "error": "开源情报数据不可用"}
