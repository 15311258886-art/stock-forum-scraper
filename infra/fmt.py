# -*- coding: utf-8 -*-
"""数字 / 百分号 / 金额格式化（P1-37：替代全仓 6 份 _num）。

所有数值展示的唯一定点：
  · to_num(raw)        —— 把任意脏输入（'1' / '1.0' / True / '' / None / 'NaN'）解析成 float 或 None
  · fmt_num(v, d=2)   —— 浮点默认 2 位，None/NaN 返回空串
  · fmt_pct(v, d=2, signed=False) —— 百分号（v 已是百分比数值，如 12.3 → "12.30%"）
  · fmt_money(v)      —— 万 / 亿 自适应（如 1.23亿）
  · fmt_yi(v)         —— 以「亿」为单位（金额字段常带 _yi 后缀）
遵循 Google-style：强类型、默认兜底、不抛异常。
"""
from __future__ import annotations

from typing import Any, Optional

_TRUEY = (True, "true", "True", "1", "yes", "是", "真")


def to_num(raw: Any) -> Optional[float]:
    """把脏输入安全解析成 float；无法解析返回 None（不抛异常）。

    覆盖全仓 6 份 _num 的分叉语义：bool→1.0、'1'→1.0、'NaN'→None、空/None→None。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        try:
            if raw != raw:  # NaN
                return None
            return float(raw)
        except (ValueError, OverflowError):
            return None
    s = str(raw).strip().replace(",", "").replace("%", "")
    if s == "" or s.lower() in ("nan", "none", "null", "-", "--"):
        return None
    if s.lower() in ("true", "false"):
        return 1.0 if s.lower() == "true" else 0.0
    try:
        return float(s)
    except (ValueError, OverflowError):
        return None


def fmt_num(v: Any, d: int = 2) -> str:
    """浮点格式化；None/NaN 返回空串（前端据此显示缺失）。"""
    n = to_num(v)
    if n is None:
        return ""
    try:
        return ("%." + str(d) + "f") % n
    except (ValueError, TypeError):
        return ""


def fmt_pct(v: Any, d: int = 2, signed: bool = False) -> str:
    """百分比字符串。v 已是百分比数值（12.3 → '12.30%'）。

    signed=True 时正数带 '+'（与 app.js:15770 的 sc>=0?'+':'' 对齐，P3-11）。
    """
    n = to_num(v)
    if n is None:
        return ""
    try:
        s = ("%+" if signed else "%") + "." + str(d) + "f"
        return (s % n) + "%"
    except (ValueError, TypeError):
        return ""


def fmt_yi(v: Any, d: int = 2) -> str:
    """金额按「亿」单位展示（金额字段常带 _yi 后缀）。"""
    n = to_num(v)
    if n is None:
        return ""
    return fmt_num(n, d) + "亿"


def fmt_money(v: Any) -> str:
    """金额自适应万 / 亿。输入单位：元。"""
    n = to_num(v)
    if n is None:
        return ""
    try:
        if abs(n) >= 1e8:
            return ("%.2f亿" % (n / 1e8))
        if abs(n) >= 1e4:
            return ("%.2f万" % (n / 1e4))
        return ("%.2f" % n)
    except (ValueError, TypeError):
        return ""
