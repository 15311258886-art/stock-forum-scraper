# -*- coding: utf-8 -*-
"""cost 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging, sqlite3
from viewer.server import (  # noqa: E402,F401
    COST_BASIS,
    COST_CALIB,
    _APP_DB,
)

def api_cost_basis() -> dict:
    """/api/cost_basis —— 成本口径表（决策①）。

    系统里有**两套都叫"成本"的数字**：回测口径 15bp（含印花税）、执行口径 25bp+
    （按 ADV 分档、不含印花税）。两个都对，但答的不是同一个问题。
    这个接口把它们并列返回，是为了让人**能自己核对**，而不是由后端挑一个"权威值"。

    纯静态定义，不读库、不计算 —— 它描述的是口径本身，不是某次运行的结果。
    """
    try:
        return COST_BASIS.describe()
    except Exception as e:  # noqa: BLE001
        logging.exception("api_cost_basis 失败")
        return {"ok": False, "error": str(e)}
def api_cost_calib() -> dict:
    """/api/cost_calib —— 成本档位标定状态（决策⑤）。

    **只读**：不改 params.COST_TIERS_BP，只给"建议值 + 样本量 + 口径限制"。
    样本不足时明确说"不构成建议"，而不是给一个凑数的数字 ——
    凑数的分档比没有分档更坏，因为它会被当成实测。
    """
    try:
        con = sqlite3.connect(_APP_DB, timeout=15)
        try:
            s = COST_CALIB.stats(con)
        finally:
            con.close()
        s["describe"] = COST_CALIB.describe()
        return s
    except Exception as e:  # noqa: BLE001
        logging.exception("api_cost_calib 失败")
        return {"ok": False, "error": str(e)}
