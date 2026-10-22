# -*- coding: utf-8 -*-
"""板块涨跌停阈值的唯一真源（P1-07 / P0-02 / P3-09）。

统一各模块的涨跌停判定，消除 screener_extra / strategy_sim / pillars 三套不一致的实现。
阈值按板块 + 时间（注册制）**时变**：
  · ST / *ST / 退市    → 5%
  · 北交所(8 开头)      → 30%
  · 创业板(300) / 科创板(688) → 20%（创业板 2019-08-24 注册制前为 10%）
  · 其余主板(60 / 00…) → 10%

历史回测必须时变：2020 年前后创业板涨跌停档位不同，固定 20% 会把历史除权/正常波动误判。
所有需要"是否涨停/跌停/可成交"判定的模块统一 import 本文件，禁止各自硬编码。
"""
from __future__ import annotations

from datetime import date

# 创业板注册制落地日（报告 P1-07 指定：此前为 10%，此后为 20%）。
CYB_REGISTRATION_DATE = date(2019, 8, 24)
# 北交所代码前缀（不含老三板 40/41/42 系列）
BJ_PREFIX = ("43", "83", "87", "88", "92")
# ST 判定关键字（名称含其一即 5% 档）
ST_KEYWORDS = ("ST", "*ST", "退", "退市")


def _is_st(name: str) -> bool:
    """名称是否含 ST / *ST / 退市 标记。"""
    n = (name or "").upper()
    return any(k in n for k in ST_KEYWORDS)


def board_of(code: str, name: str = "") -> str:
    """粗略板块判定（与 strategy_sim.board_of 同口径，独立实现避免循环依赖）。"""
    c = str(code or "").strip()
    if _is_st(name):
        return "风险警示板"
    if c.startswith(BJ_PREFIX):
        return "北交所"
    if c.startswith(("688", "689")):
        return "科创板"
    if c.startswith("300"):
        return "创业板"
    if c.startswith(("60", "9")):
        return "沪市主板"
    if c.startswith(("00", "001", "002", "003", "30")):
        return "深市主板"
    return "未知板块"


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:  # noqa: BLE001
        return None


def limit_pct(code: str, name: str = "", asof: str | None = None) -> float:
    """返回该票在 asof(YYYY-MM-DD) 当日的涨跌停幅度（小数，如 0.30/0.20/0.10/0.05）。

    - ST / *ST / 退市                  → 0.05
    - 北交所(8 开头)                    → 0.30
    - 科创板(688)                      → 0.20
    - 创业板(300)：注册制落地(2019-08-24)后 → 0.20，之前 → 0.10
    - 其余主板                          → 0.10
    """
    if _is_st(name):
        return 0.05
    c = str(code or "").strip()
    if c.startswith(BJ_PREFIX):
        return 0.30
    d = _parse_date(asof)
    if c.startswith(("688", "689")):
        return 0.20
    if c.startswith("300"):
        # 创业板时变：注册制前 10%，之后 20%
        if d is not None and d >= CYB_REGISTRATION_DATE:
            return 0.20
        return 0.10
    return 0.10


def is_limit_up(pct: float, code: str, name: str = "", asof: str | None = None) -> bool:
    """给定当日涨跌幅 pct(%) 是否封涨停（按板块阈值 ≥ ~98.5%）。"""
    lp = limit_pct(code, name, asof)
    return pct >= lp * 100.0 * 0.985


def is_limit_down(pct: float, code: str, name: str = "", asof: str | None = None) -> bool:
    """给定当日涨跌幅 pct(%) 是否封跌停（按板块阈值 ≤ ~-98.5%）。"""
    lp = limit_pct(code, name, asof)
    return pct <= -lp * 100.0 * 0.985


def tradeable(bar, side: str, code: str, name: str = "", asof: str | None = None) -> bool:
    """统一「可否成交」判定（P1-03 公共口径）。

    所有 ret/label 路径在计算收益前都先过此函数：涨停不可买、跌停不可卖、停牌不可成交。
    - bar: dict，须含 pct(当日涨跌幅 %)、volume(成交量)、suspended(可选)；
           缺失 pct 但非停牌视为可成交（按 bar 已有信息尽力判断）。
    - side: "BUY"（或含 "买"）取买入约束；其它取卖出约束。
    - 返回 True = 可成交（收益可达、应计入样本）；False = 不可成交（应跳过，避免把
      "涨停买不进 / 跌停卖不出" 误算成命中或亏损）。
    """
    if bar is None:
        return False
    if bar.get("suspended") is True or (bar.get("volume", 0) or 0) <= 0:
        return False
    pct = bar.get("pct")
    if pct is None:
        return True
    if str(side).upper().startswith("B") or "买" in str(side):
        return not is_limit_up(pct, code, name, asof)
    return not is_limit_down(pct, code, name, asof)
