# -*- coding: utf-8 -*-
"""executability 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging, sqlite3
from viewer.server import (  # noqa: E402,F401
    EXECUTABILITY,
    _APP_DB,
)

def api_executability(codes, aum, n_hold, hold_days, fee_bp) -> dict:
    """/api/executability?codes=...&aum=...&n=...&days=...&fee=...

    四字段必填。缺任一项返回 missing_required，**不用默认值顶替** ——
    用默认值算出来的可行性，是给别人看的可行性。

    2026-10-31 决策④：`days` 支持多值（`days=3,10,20`），此时返回
    `{scenarios: [...]}` 三档并排；单值仍返回单档完整结果（向后兼容）。
    进哥说不出自己典型持有几天、也不该为此去翻交割单，所以让他认领一档。
    """
    cl = [c.strip() for c in str(codes or "").replace(";", ",").split(",") if c.strip()]
    names = {}
    if cl:
        try:
            con = sqlite3.connect(_APP_DB, timeout=15)
            try:
                qs = ",".join("?" * len(cl))
                for code, name in con.execute(
                        "SELECT code, name FROM stock_daily_snapshot WHERE code IN (%s) "
                        "AND trade_date=(SELECT MAX(trade_date) FROM stock_daily_snapshot)"
                        % qs, cl).fetchall():
                    names[code] = name
            finally:
                con.close()
        except Exception:  # noqa: BLE001
            logging.exception("api_executability 取名失败（已忽略，仅影响 ST 判定）")
    hs = [x.strip() for x in str(hold_days or "").replace(";", ",").split(",") if x.strip()]
    if len(hs) > 1:
        return EXECUTABILITY.sweep_hold_days(cl, aum=aum, n_hold=n_hold, fee_bp=fee_bp,
                                             hold_days_list=hs, names=names)
    return EXECUTABILITY.evaluate(cl, aum=aum, n_hold=n_hold,
                                  hold_days=(hs[0] if hs else hold_days),
                                  fee_bp=fee_bp, names=names)
