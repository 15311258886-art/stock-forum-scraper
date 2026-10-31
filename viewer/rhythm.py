# -*- coding: utf-8 -*-
"""交易节奏策略（2026-10-30 按用户「盘后复盘优先」的节奏重构）。

【为什么需要这个模块】
进哥（用户）的真实工作节奏是「盘中几乎不下单、收盘后复盘分析、第二天盘中找时机买」，
原话：「盘中的实时性对他没有价值，一个小时更新一次都行」。

而重构前的实现是把「实时」当成默认目标，于是每个业务文件里各自散写时段判断
（`if 9 <= hour <= 15` 之类），口径互相漂移，后果是：
  · 收盘后（正是用户复盘、最需要快的时候）系统还在按「盘中」的节奏每 30 秒采一次全板块
    快照、每次进面板都重抓 16 个板块的东财分钟线（实测冷态 7.5s）；
  · 盘中又没有任何地方明确说「一小时一次就够」，于是抓得越多越挨风控、越慢。
本模块把「什么时段该多快」收敛成**唯一真源**：业务文件只问本模块，不再自己判断，
避免半年后出现「两个文件对同一个时段给出两个答案」这种只能靠翻日志才能发现的漂移。

【对外契约】
  phase(now)                  'preopen' | 'trading' | 'lunch' | 'closed'
  is_trading_day_now(now)     今天是不是交易日（唯一权威源 auction.is_trading_day）
  is_after_close(now)         交易日 15:05 之后（当日数据已定稿）
  intraday_sample_gap()       板块资金流采样间隔（秒），0 = 不采样
  intraday_curve_ttl()        分时曲线内存缓存 TTL（秒）
  intraday_live_fetch_gap()   允许重打东财分钟接口的最小间隔（秒），0 = 定稿后不再抓
  refresh_note()              给前端的一句话（如「盘中·每小时刷新」）

【纪律】
  1. 只用 auction 一个日历权威源，本模块**不自己造交易日历**（2026-10-03 的教训：
     两套日历漂移，导致 09-30 的曲线被盖上 10-03 的日期戳）。
  2. 所有函数**绝不允许抛异常**，也绝不允许在非交易日下返回错误结果——
     它们被调用在请求路径上，抛一次就是 500。
  3. 日历取不到（auction 返回 None，即「未知」）时按**工作日**保守处理：
     宁可多采一轮，也不能让系统在工作日装死（旧实现把未知当休市才踩过坑）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

# auction 在项目根、本文件在 viewer/ 下：沿用 board_flow.py 的稳妥写法补 sys.path，
# 保证「python -m viewer.server」与「从项目根 import viewer.rhythm」两种入口都能导入，
# 且不会形成 viewer ↔ 根模块的循环导入（auction 不 import 任何本项目模块）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:  # noqa: E402 —— 导入失败不能炸掉引用方，退化为「工作日」口径
    from auction import is_trading_day as _auction_is_trading_day
except Exception:  # noqa: BLE001
    _auction_is_trading_day = None  # type: ignore[assignment]


# ---------------------------------------------------------------- 时段边界（分钟数）
_PREOPEN_START = 9 * 60 + 0        # 09:00 盘前
_OPEN = 9 * 60 + 30                # 09:30 开盘
_MORNING_END = 11 * 60 + 30        # 11:30 上午收盘
_AFTERNOON_START = 13 * 60 + 0     # 13:00 下午开盘
_CLOSE = 15 * 60 + 0               # 15:00 收盘
_AFTER_CLOSE = 15 * 60 + 5         # 15:05 当日数据定稿（与 board_flow 采样窗口收尾一致）

# ---------------------------------------------------------------- 各时段的节奏参数（秒）
_GAP_TRADING = 1800        # 盘中采样间隔：30 分钟（用户明确「一小时一次都行」，取一半留余量）
_GAP_OFFHOURS = 1800       # 盘前/午休：同上（这两个时段本来也基本采不到新值）
_GAP_CLOSED = 0            # 盘后/休市：不采样

_TTL_TRADING = 3600        # 盘中曲线缓存：1 小时（用户明确接受的粒度）
_TTL_CLOSED = 86400        # 盘后：24 小时（曲线已定稿，重算无信息增益）

_LIVE_TRADING = 3600       # 盘中允许重打东财分钟接口的最小间隔：1 小时
_LIVE_CLOSED = 0           # 盘后：0 = 定稿后不再抓（见 board_flow._live_fetch_gate 的「每天一次」兜底）

_NOTE_TRADING = "盘中·每小时刷新"
_NOTE_LUNCH = "午间休市·每小时刷新"
_NOTE_PREOPEN = "盘前·每小时刷新"
_NOTE_FINAL = "收盘定稿·不再实时抓取"
_NOTE_PENDING = "收盘·等当日数据定稿"
_NOTE_HOLIDAY = "休市·最近交易日数据"


def _as_dt(now=None) -> datetime:
    """把入参统一成 datetime；None → 当前本地时间。

    允许传 datetime / date / "YYYY-MM-DD HH:MM[:SS]" / "HH:MM"（自检脚本与单测用），
    任何解析失败一律回落到「当前时间」——本模块的函数不允许因为参数脏而抛异常。
    """
    if now is None:
        return datetime.now()
    if isinstance(now, datetime):
        return now
    try:
        if hasattr(now, "year") and hasattr(now, "month") and hasattr(now, "day"):
            # datetime.date（无时分）→ 当天 00:00
            return datetime(now.year, now.month, now.day)
        s = str(now).strip()
        if not s:
            return datetime.now()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%H:%M:%S", "%H:%M"):
            try:
                dt = datetime.strptime(s, fmt)
            except ValueError:
                continue
            if fmt in ("%H:%M:%S", "%H:%M"):
                # 只给时分 → 贴到「今天」这个日期上，方便自检脚本描述「一天中的某个时刻」
                d = datetime.now()
                return dt.replace(year=d.year, month=d.month, day=d.day)
            return dt
    except Exception:  # noqa: BLE001
        return datetime.now()
    return datetime.now()


def is_trading_day_now(now=None) -> bool:
    """今天是不是交易日（唯一权威源：auction.is_trading_day）。

    auction 返回 None 表示「日历取不到（未知）」——此时按**工作日**保守处理：
      · 若按休市处理，工作日一整天不采样、不抓曲线，面板会整个白天停在昨天的数据上
        （且没有任何错误提示，比慢更糟）；
      · 按工作日处理最多在法定节假日多跑一次空轮（新浪快照给的是上一交易日值，
        board_flow.maybe_sample 自身的 `is_trading_day() is False` 闸仍会拦住落库）。
    """
    dt = _as_dt(now)
    if _auction_is_trading_day is None:
        return dt.weekday() < 5
    try:
        v = _auction_is_trading_day(dt.strftime("%Y-%m-%d"))
    except Exception:  # noqa: BLE001 —— 日历源异常不许冒泡到请求路径
        return dt.weekday() < 5
    if v is None:
        return dt.weekday() < 5
    return bool(v)


def phase(now=None) -> str:
    """返回 'preopen' | 'trading' | 'lunch' | 'closed'。

    trading: 交易日 09:30–11:30、13:00–15:00（含 15:00 那一刻，收盘瞬间仍算盘中）；
    lunch:   交易日 11:30–13:00；
    preopen: 交易日 09:00–09:30；
    其余（含休市全天、交易日 09:00 之前与 15:00 之后）为 closed。
    """
    try:
        dt = _as_dt(now)
        if not is_trading_day_now(dt):
            return "closed"
        hm = dt.hour * 60 + dt.minute
        if _PREOPEN_START <= hm < _OPEN:
            return "preopen"
        if _OPEN <= hm < _MORNING_END:
            return "trading"
        if _MORNING_END <= hm < _AFTERNOON_START:
            return "lunch"
        if _AFTERNOON_START <= hm <= _CLOSE:
            return "trading"
        return "closed"
    except Exception:  # noqa: BLE001
        return "closed"


def is_after_close(now=None) -> bool:
    """交易日 15:05 之后（当日数据已定稿）。非交易日恒为 False。

    15:05 这个点与 board_flow 的采样窗口收尾（SAMPLE_END_MIN = 15:05）对齐：
    收盘后 5 分钟内东财分钟接口还会补最后一笔，之后当日曲线不再变化。
    """
    try:
        dt = _as_dt(now)
        if not is_trading_day_now(dt):
            return False
        return (dt.hour * 60 + dt.minute) >= _AFTER_CLOSE
    except Exception:  # noqa: BLE001
        return False


def intraday_sample_gap(now=None) -> int:
    """板块资金流采样间隔（秒）。盘中 1800，午休/盘前 1800，盘后 0（不采样）。

    0 的含义是「本时段不要采样」，调用方必须显式 `if gap <= 0: return 0`，
    **不能**直接拿 0 去比较「距上次采样是否超过 gap」（那会变成每轮都采）。

    可选参数 now 只为自检/单测用（不传 = 当前时间），业务代码一律不传。
    """
    try:
        ph = phase(now)
        if ph == "closed":
            return _GAP_CLOSED
        if ph == "trading":
            return _GAP_TRADING
        return _GAP_OFFHOURS
    except Exception:  # noqa: BLE001
        return _GAP_OFFHOURS


def intraday_curve_ttl(now=None) -> int:
    """分时曲线缓存 TTL（秒）。盘中 3600（用户明确接受一小时），收盘定稿后 86400。

    ⚠️ 只有「交易日 15:05 之后（is_after_close）」才给 86400。这个区分是必须的：
      盘前（08:00~09:30）phase 同样是 closed，但那时**今天根本没有曲线**，
      缓存里放的会是「上一交易日」的曲线；若给它 24 小时 TTL，整个交易日都会命中
      昨天的曲线（盘中用户看到的永远是昨天）。所以盘前/收盘结算中一律只给 3600，
      让缓存跨过 09:30 必然过期，开盘后重新抓今天的。

    可选参数 now 只为自检/单测用（不传 = 当前时间）。
    """
    try:
        if phase(now) != "closed":
            return _TTL_TRADING
        return _TTL_CLOSED if is_after_close(now) else _TTL_TRADING
    except Exception:  # noqa: BLE001
        return _TTL_TRADING


def intraday_live_fetch_gap(now=None) -> int:
    """允许重新打东财分钟接口的最小间隔（秒）。盘中 3600，盘后 0 表示「定稿后不再抓」。

    可选参数 now 只为自检/单测用（不传 = 当前时间）。
    """
    try:
        return _LIVE_CLOSED if phase(now) == "closed" else _LIVE_TRADING
    except Exception:  # noqa: BLE001
        return _LIVE_TRADING


def refresh_note(now=None) -> str:
    """给前端的一句话（board_flow 拼进 market_note，前端 app.js 已直接渲染该字段）。

    可选参数 now 只为自检/单测用（不传 = 当前时间）。
    """
    try:
        dt = _as_dt(now)
        if not is_trading_day_now(dt):
            return _NOTE_HOLIDAY
        ph = phase(dt)
        if ph == "trading":
            return _NOTE_TRADING
        if ph == "lunch":
            return _NOTE_LUNCH
        if ph == "preopen":
            return _NOTE_PREOPEN
        # closed：交易日里区分「15:05 后已定稿」「15:00~15:05 收盘结算中」「09:00 前盘前」
        # （15:00–15:05 这 5 分钟若是闭市口径，说「盘前」会误导用户，故单列一条）
        if is_after_close(dt):
            return _NOTE_FINAL
        hm = dt.hour * 60 + dt.minute
        return _NOTE_PREOPEN if hm < _OPEN else _NOTE_PENDING
    except Exception:  # noqa: BLE001
        return _NOTE_HOLIDAY


def describe(now=None) -> dict:
    """一张表把当前节奏拆开（供 /api/rhythm、自检脚本、日志排查用）。绝不抛异常。"""
    dt = _as_dt(now)
    try:
        ph = phase(dt)
    except Exception:  # noqa: BLE001
        ph = "closed"
    out = {"now": dt.strftime("%Y-%m-%d %H:%M:%S"), "phase": ph}
    for key, fn in (("is_trading_day", is_trading_day_now),
                    ("is_after_close", is_after_close)):
        try:
            out[key] = bool(fn(dt))
        except Exception:  # noqa: BLE001
            out[key] = False
    for key, fn in (("sample_gap", intraday_sample_gap),
                    ("curve_ttl", intraday_curve_ttl),
                    ("live_fetch_gap", intraday_live_fetch_gap)):
        try:
            out[key] = int(fn(dt))
        except Exception:  # noqa: BLE001
            out[key] = 0
    try:
        out["note"] = refresh_note(dt)
    except Exception:  # noqa: BLE001
        out["note"] = ""
    return out
