# -*- coding: utf-8 -*-
"""review 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_review_score() -> dict:
    """AI 复盘成绩单（2026-10-31）：**由系统按收盘价回填，不是模型自评**。

    这是「AI 说的话到底准不准」唯一可信的来源。口径：复盘日次一交易日收盘建仓，
    T+5/T+10 收盘计，超额相对同日全市场等权。

    注意 `min_sample_ok`：有效样本 <30 条时为 False，前端必须把命中率显示成
    「趋势」而不是「结论」—— 6 个交易日算出来的百分比不配叫准确率。
    """
    try:
        import review_score as RS
        d = RS.report(DB.conn)
        d["ok"] = True
        d["text"] = RS.scoreboard_text(DB.conn)
        return d
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": str(e)[:160]}
