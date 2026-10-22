# -*- coding: utf-8 -*-
"""统一时间源（P2-06 / P2-05）：所有「当前时间」调用收口到 now_cst()，
避免 naive datetime.now() 受本机系统时区影响，导致竞价窗口 / 交易日判断错乱。

后果说明（体检报告 P2-06）：时区错 → 竞价窗口（9:15–9:35）永不命中 → 9:25 数据永久缺失
（auction 模块文档自述「那一刻之后再拿不回」）。全仓 194 处 naive datetime.now()
应逐步替换为本模块；本批次先覆盖算法/数据域内的关键路径。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    _CST = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 —— 极老环境无 zoneinfo 时退回固定 +8
    _CST = timezone(timedelta(hours=8))


def now_cst() -> datetime:
    """当前北京时间（带时区感知）。"""
    return datetime.now(_CST)


def now_cst_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """当前北京时间，按 fmt 格式化为字符串（默认 'YYYY-MM-DD HH:MM:SS'）。"""
    return now_cst().strftime(fmt)


def today_cst() -> str:
    """当前北京日期 'YYYY-MM-DD'。"""
    return now_cst().strftime("%Y-%m-%d")
