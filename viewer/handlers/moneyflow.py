# -*- coding: utf-8 -*-
"""moneyflow 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    _stock_moneyflow_any,
    moneyflow,
)

def api_moneyflow_stock(symbol: str) -> dict:
    # 2026-09-29 修复：本机东财 fflow 接口常不可达，改走 _stock_moneyflow_any
    # （东财主源 → 新浪兜底），与 api_diag / api_board ③ 一致，避免个股资金流整页空。
    return _stock_moneyflow_any(symbol)
def api_moneyflow_stock_force(symbol: str, force: bool = True) -> dict:
    """个股资金流 + 强制重试（跳过 30 分钟失败负缓存）。"""
    return _stock_moneyflow_any(symbol, force=force)
def api_moneyflow_rank(pz: int = 30, force: bool = False) -> dict:
    """2026-10-01：force 透传 —— 前端「强制重试」要能绕过 30 分钟失败负缓存。"""
    return moneyflow.rank(pz=pz, force=force)
def api_moneyflow_board(pz: int = 30, force: bool = False) -> dict:
    """同上：force=True 跳过 moneyflow.board_rank 的失败负缓存。"""
    return moneyflow.board_rank(pz=pz, force=force)
