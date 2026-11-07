# -*- coding: utf-8 -*-
"""shadow 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging, sqlite3
from viewer.server import (  # noqa: E402,F401
    _APP_DB,
    predictor,
)

def api_shadow_weights(days: int = 20) -> dict:
    """/api/shadow_weights —— 影子权重 vs 生产权重（决策②）。

    影子权重是"如果不用先验、纯按数据排"的结果，每日记录，**不生效**。
    这个接口只做对比展示；切换与否由人按 switch_rule 判断。
    """
    try:
        con = sqlite3.connect(_APP_DB, timeout=15)
        try:
            return predictor.load_shadow(con, days)
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        logging.exception("api_shadow_weights 失败")
        return {"ok": False, "error": str(e)[:200]}
