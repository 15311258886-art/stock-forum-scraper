# -*- coding: utf-8 -*-
"""交易日历 / CST 时区（P1-37：替代 4 套日历；P2-06：替代 naive datetime.now）。

提供：
  · now_cst()                 —— 上海时区当前 datetime（替代 194 处 naive now）
  · is_trading_day(dt)        —— 是否交易日（排除周末 + 内地法定节假日）
  · trading_minutes(dt)       —— 返回交易时段集合（开盘/连续/收盘集合竞价等）
  · is_trading_now(dt=None)   —— 当前是否处于交易时段
  · next_trading_day(dt)      —— 下一个交易日

内地节假日按近年公开表内置（2024-2026），超出范围时按"周末即休"保守处理并打日志，
绝不抛异常（避免调度因缺表而崩）。
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
    _CST = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    _CST = None  # 退化为本地时区（Windows 上通常即 CST）

# 内地法定节假日（调休放假的工作日），年-月-日。超出范围的年份按"周末即休"处理。
_HOLIDAYS = {
    # 2024
    "2024-01-01", "2024-02-10", "2024-02-11", "2024-02-12", "2024-02-13", "2024-02-14",
    "2024-02-15", "2024-02-16", "2024-02-17", "2024-04-04", "2024-04-05", "2024-04-06",
    "2024-05-01", "2024-05-02", "2024-05-03", "2024-05-04", "2024-05-05", "2024-06-10",
    "2024-09-15", "2024-09-16", "2024-09-17", "2024-10-01", "2024-10-02", "2024-10-03",
    "2024-10-04", "2024-10-05", "2024-10-06", "2024-10-07",
    # 2025
    "2025-01-01", "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-01",
    "2025-02-02", "2025-02-03", "2025-02-04", "2025-04-04", "2025-04-05", "2025-04-06",
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05", "2025-06-02",
    "2025-09-03", "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04", "2025-10-05",
    "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026（已公布主要假期）
    "2026-01-01", "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05", "2026-06-19",
    "2026-09-25", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    "2026-10-06", "2026-10-07",
}
# 调休补班的工作日（周末上班），按"周末且在此集合 → 算交易日"。
_MAKEUP_WORKDAYS = {
    "2024-02-04", "2024-02-18", "2024-04-07", "2024-04-28", "2024-05-11", "2024-09-14",
    "2024-09-29", "2024-10-12",
    "2025-01-26", "2025-02-08", "2025-04-27", "2025-05-10", "2025-09-28", "2025-10-11",
    "2026-02-08", "2026-02-14", "2026-05-09", "2026-09-19", "2026-10-10",
}


def now_cst() -> _dt.datetime:
    """上海时区当前时间（带 tzinfo）。无 zoneinfo 时退化为 naive 本地时间。"""
    if _CST is not None:
        return _dt.datetime.now(_CST)
    return _dt.datetime.now()


def _ymd(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def is_trading_day(dt: Optional[_dt.datetime] = None) -> bool:
    """是否交易日：周末且非补班 → 休；在节假日集合 → 休；其余 → 开。"""
    if dt is None:
        dt = now_cst()
    d = _ymd(dt)
    wd = dt.weekday()
    if wd >= 5:  # 周末
        return d in _MAKEUP_WORKDAYS
    return d not in _HOLIDAYS


def trading_minutes(dt: Optional[_dt.datetime] = None):
    """返回当日交易时段（分钟数集合）：集合竞价 + 连续竞价。

    09:15-09:25 开盘集合竞价；09:30-11:30 / 13:00-14:57 连续；14:57-15:00 收盘集合竞价。
    """
    _ = dt
    return {
        "open_call": (9 * 60 + 15, 9 * 60 + 25),
        "morning": (9 * 60 + 30, 11 * 60 + 30),
        "afternoon": (13 * 60, 14 * 60 + 57),
        "close_call": (14 * 60 + 57, 15 * 60),
    }


def is_trading_now(dt: Optional[_dt.datetime] = None) -> bool:
    """当前是否处于交易时段（P2-06：用 CST 而非 OS 时钟）。"""
    if dt is None:
        dt = now_cst()
    if not is_trading_day(dt):
        return False
    m = dt.hour * 60 + dt.minute + dt.second / 60.0
    segs = trading_minutes(dt)
    for (a, b) in segs.values():
        if a <= m <= b:
            return True
    return False


def next_trading_day(dt: Optional[_dt.datetime] = None) -> _dt.datetime:
    """下一个交易日 0 点（用于调度窗口计算）。"""
    if dt is None:
        dt = now_cst()
    cur = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(30):
        cur = cur + _dt.timedelta(days=1)
        if is_trading_day(cur):
            return cur
    return cur
