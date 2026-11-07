# -*- coding: utf-8 -*-
"""hypothesis 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_hypothesis(limit: int = 30) -> dict:
    """假说回测记录（2026-10-31）：模型提的假设 + 系统给出的检验结论。

    四种状态：confirmed（成立）/ opposite（**方向相反**，最有价值的一类）/
    unsupported（没测出效应）/ invalid（谓词无法回测）。
    """
    try:
        import hypo as HP
        d = HP.report(DB.conn, max(1, min(int(limit or 30), 200)))
        d["ok"] = True
        return d
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": str(e)[:160]}
