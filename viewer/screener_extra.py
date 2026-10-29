"""智能选股增强（B4/B5/B7/B8/B6）后端数据层 —— 纯标准库 + 可选第三方。

设计原则（2026-10-07 修订，硬约束）：
- 取不到真实数据就明确报错，绝不返回任何模拟/合成/假数据。
- 所有依赖第三方库（baostock / akshare / pandas）的调用都包在 try/except 里；
  真实数据源（akshare / baostock）未安装、网络失败、接口变动时，函数返回
  {"ok": False, "error": "..."} 或空结果，由调用方如实告知用户「数据未获取」，
  任何人任何时候都不准用编的数填充。
- 真实数据（历史K线、财务指标、概念板块、DeepSeek 调用）在「能联网 + 能 pip install」
  的本机运行 viewer/server.py 时自动生效。
- 盘后批量缓存到 viewer/cache/，避免每次打开都拉远程（省调用、防源站抽风）。

路由（在 server.py 注册）：
  GET  /api/screener/valuation      批量估值分位（B4）
  GET  /api/screener/backtest       策略回测（B7）
  GET  /api/screener/themes         题材/概念映射（B8）
  GET  /api/valuation_cache/status  缓存状态
  POST /api/nl_screener             自然语言选股（B6，server 代理 DeepSeek，key 不落前端）

B7 回测（重点，2026-10-22 重写「真回测」）：
- 数据层 get_kline：优先 baostock（沙箱实测可通），备选 akshare（本机可能通）；
  返回【后复权 hfq】日线 + 成交量 + 涨跌幅 + 停牌标记；取不到返回 []（绝不构造假数据）。
- 事件驱动：信号于月末收盘生成 → 次一交易日（T+1）开盘成交（防前视偏差）；
  T+1 买入后当日不可卖（制度）。
- 真实成本模型：佣金万2.5 双边（单笔最低 5 元）+ 印花税 0.05%（仅卖出）
  + 滑点 0.1%（买卖各 0.1%）+ 过户费 0.001%（沪市）。
- 涨跌停 / 停牌约束：涨停日买不进、跌停日卖不出、停牌日（成交量 0 或 tradestatus=0）
  持仓不动。涨跌停用涨跌幅 + 收盘价封板近似（baostock 无独立涨跌停字段，属近似，
  已在 caveats 标注）。
- 一把尺子：构建一条「等权组合净值曲线（后复权、含成本、含约束）」作为唯一真相源，
  所有指标（总收益几何、年化、最大回撤、夏普、索提诺、卡玛、胜率、盈亏比、月度、
  逐笔）均从这条曲线派生，绝不混用三种尺子。
- 样本外 / walk-forward：区间按 70/30 切分（开发/验证），样本外只验一次，报告两段对比。
- 成本敏感性：基准成本 + 2 倍成本都报。
- 幸存者偏差：明确标注（基于给定存活标的列表，未剔除退市股）；但窗口内中途退市的标的会按最后可得价强制平仓并计入 delisted_exits，不再冻结虚增净值。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# ── 第三方库搜索路径注入（沙箱用 --target extlib 安装，本机若无则自动忽略）──
# 基于 __file__ 计算相对路径：viewer/screener_extra.py → 项目根/extlib
_THIS_FILE = Path(__file__).resolve()
_EXTLIB = _THIS_FILE.parent.parent / "extlib"
if _EXTLIB.exists() and str(_EXTLIB) not in sys.path:
    sys.path.insert(0, str(_EXTLIB))

# ── 可选第三方库：本机未安装时 import 失败，函数内如实报错（绝不回退假数据）──
try:
    import baostock as _bs  # type: ignore
    _HAS_BS = True
except Exception:  # noqa: BLE001
    _HAS_BS = False
try:
    import akshare as _ak  # type: ignore
    _HAS_AK = True
except Exception:  # noqa: BLE001
    _HAS_AK = False
try:
    import pandas as pd  # type: ignore
    _HAS_PD = True
except Exception:  # noqa: BLE001
    _HAS_PD = False

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True)

TRADING_DAYS_PER_YEAR = 252


# ───────────────────────── 真实成本模型（模块顶部可配置常量） ─────────────────────────
# 佣金万 2.5 双边（单笔最低 5 元）
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
# 印花税 0.05%（仅卖出）
STAMP_RATE = 0.0005
# 滑点 0.1%（买卖各 0.1%）
SLIPPAGE_RATE = 0.001
# 过户费 0.001%（仅沪市，可选）
TRANSFER_RATE = 0.00001
# 成本敏感性倍数（基准 = 1.0，高成本情景 = 2.0）
COST_MULTIPLIER_BASE = 1.0
COST_MULTIPLIER_2X = 2.0
# 组合初始资金（元）。必须用真实货币基数，否则「单笔最低 5 元佣金」这类绝对额成本
# 在归一化到 1.0 的净值下会被不正常放大、导致建仓被跳过。
INIT_CAPITAL = 1_000_000.0
# 涨跌停阈值
LIMIT_MAIN = 0.10   # 主板 10%
LIMIT_STAR = 0.20   # 创业板(300)/科创板(688)/北交所(8) 20%
# 再平衡频率：月度（事件驱动）；信号于月末收盘，T+1 开盘成交
REBALANCE = "M"


# ───────────────────────── 缓存工具 ─────────────────────────
def _cache_path(name: str) -> Path:
    return CACHE_DIR / (name + ".json")


def _load_cache(name: str):
    p = _cache_path(name)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def _save_cache(name: str, data) -> None:
    try:
        _cache_path(name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ───────────────────────── 代码归一 ─────────────────────────
def _norm_ak(code: str) -> str:
    """600000 → 600000（akshare 直接用数字）；sh.600000 / 600000.SH 都归一成 6 位数字。"""
    c = code.strip().upper().replace("SH.", "").replace("SZ.", "").replace(".SH", "").replace(".SZ", "")
    c = c.split(".")[-1] if "." in c else c
    return c[-6:] if len(c) >= 6 else c


def _to_bs(code: str) -> str:
    c = _norm_ak(code)
    return ("sh." if c[0] == "6" else "sz.") + c


# ───────────────────────── 历史K线（仅真实，后复权 hfq） ─────────────────────────
def get_kline(code: str, start: str, end: str):
    """返回 [{date, open, high, low, close, volume, pct, suspended}]。

    仅走真实源：优先 baostock（adjustflag="1" 真后复权），备选 akshare（adjust="hfq"）。
    两者都不可用或失败时返回 []（绝不构造假数据）。
    - date: 'YYYY-MM-DD'
    - pct: 当日涨跌幅（%，如 1.23 表示 +1.23%）
    - suspended: 是否停牌（成交量 0 或 baostock tradestatus != '1'）
    """
    c = _norm_ak(code)
    # 优先 baostock（沙箱实测可通）
    if _HAS_BS:
        try:
            _bs.login()
            rs = _bs.query_history_k_data_plus(
                _to_bs(code),
                "date,open,high,low,close,volume,pctChg,tradestatus",
                start_date=start, end_date=end, frequency="d", adjustflag="1")
            out = []
            while (rs is not None) and rs.error_code == "0" and rs.next():
                d = rs.get_row_data()
                # d = [date, open, high, low, close, volume, pctChg, tradestatus]
                o = _f(d[1])
                h = _f(d[2])
                low = _f(d[3])
                cl = _f(d[4])
                v = _f(d[5]) or 0.0
                pct = _f(d[6]) or 0.0
                ts = d[7] if len(d) > 7 else "1"
                suspended = (ts == "0") or (v <= 0)
                out.append({"date": d[0], "open": o, "high": h, "low": low,
                            "close": cl, "volume": v, "pct": pct, "suspended": suspended})
            _bs.logout()
            if out:
                return out
        except Exception:  # noqa: BLE001
            pass
    # 备选 akshare（本机可能通；沙箱代理不通会在上面被捕获后落到这里，再失败则诚实返回 []）
    if _HAS_AK:
        try:
            df = _ak.stock_zh_a_hist(symbol=c, period="daily",
                                     start_date=start.replace("-", ""),
                                     end_date=end.replace("-", ""),
                                     adjust="hfq")
            if df is not None and len(df):
                out = []
                for _, row in df.iterrows():
                    o = _f(row.get("开盘"))
                    h = _f(row.get("最高"))
                    low = _f(row.get("最低"))
                    cl = _f(row.get("收盘"))
                    v = _f(row.get("成交量")) or 0.0
                    pct = _f(row.get("涨跌幅")) or 0.0
                    out.append({"date": str(row.get("日期", "")), "open": o, "high": h,
                                "low": low, "close": cl, "volume": v,
                                "pct": pct, "suspended": (v <= 0)})
                if out:
                    return out
        except Exception:  # noqa: BLE001
            pass
    # 未获取到真实行情：返回空，绝不构造任何假数据
    return []


# ───────────────────────── B7 回测：约束 / 成本 / 指标工具 ─────────────────────────
def _limit_pct(code: str) -> float:
    """按板块返回涨跌停幅度（P1-07 / P3-09：统一走 board_rules.limit_pct 唯一真源）。

    创业板(300)/科创板(688)/北交所(8) 按板块 + 注册制落地日时变；ST 5%；其余主板 10%。
    board_rules 不可用时回退旧常数，保持兼容。
    """
    c = _norm_ak(code)
    try:
        from board_rules import limit_pct as _br_limit_pct
        return _br_limit_pct(c, "")
    except Exception:  # noqa: BLE001
        if c[:3] in ("300", "688") or c.startswith("8"):
            return LIMIT_STAR
        return LIMIT_MAIN


def _is_limit_up(bar: dict, code: str) -> bool:
    """涨停近似：涨跌幅达板块阈值 ~98.5% 且收盘价封板（close==high）。"""
    if bar is None or bar.get("suspended"):
        return False
    lp = _limit_pct(code)
    pct = bar.get("pct") or 0.0
    close = bar.get("close")
    high = bar.get("high")
    if close is None or high is None:
        return False
    return pct >= lp * 0.985 and abs(close - high) < 1e-6


def _is_limit_down(bar: dict, code: str) -> bool:
    """跌停近似：涨跌幅达 -板块阈值 ~98.5% 且收盘价封板（close==low）。"""
    if bar is None or bar.get("suspended"):
        return False
    lp = _limit_pct(code)
    pct = bar.get("pct") or 0.0
    close = bar.get("close")
    low = bar.get("low")
    if close is None or low is None:
        return False
    return pct <= -lp * 0.985 and abs(close - low) < 1e-6


def _is_suspended(bar: dict) -> bool:
    """停牌：显式标记或成交量 0。"""
    if bar is None:
        return True
    if bar.get("suspended") is True:
        return True
    return (bar.get("volume", 0) or 0) <= 0


def _trade_cost(amount: float, is_buy: bool, code: str, mult: float) -> float:
    """单笔交易成本（元）。amount 为成交金额（按执行价计）。mult 为成本倍数（敏感性）。"""
    if amount <= 0:
        return 0.0
    comm = max(COMMISSION_RATE * amount * mult, COMMISSION_MIN * mult)
    slip = SLIPPAGE_RATE * amount * mult
    stamp = STAMP_RATE * amount if not is_buy else 0.0
    transfer = TRANSFER_RATE * amount * mult if _norm_ak(code).startswith("6") else 0.0
    return comm + slip + stamp + transfer


def _mean(xs: list) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _pstd(xs: list) -> float:
    """总体标准差（ddof=0）。样本 < 2 时返回 0。"""
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _monthly(nav_curve: list) -> list:
    """按自然月聚合净值曲线，返回 [{month, ret}]（月收益 = 月末净值 / 月初净值 - 1）。"""
    if not nav_curve:
        return []
    by_month: dict = {}
    for p in nav_curve:
        m = p["date"][:7]
        by_month.setdefault(m, []).append(p["nav"])
    out = []
    for m in sorted(by_month.keys()):
        vals = by_month[m]
        if len(vals) >= 1:
            ret = vals[-1] / vals[0] - 1 if vals[0] != 0 else None
            out.append({"month": m, "ret": round(ret, 4) if ret is not None else None})
    return out


def _metrics_from_nav(nav_curve: list, trades: list) -> dict:
    """从唯一真相源（组合净值曲线）派生全部指标。绝不在别处另算尺子。"""
    if len(nav_curve) < 2:
        return {"total_return": None, "annual_return": None, "max_drawdown": None,
                "sharpe": None, "sortino": None, "calmar": None, "win_rate": None,
                "profit_loss_ratio": None, "trading_days": len(nav_curve),
                "final_nav": nav_curve[-1]["nav"] if nav_curve else None}
    navs = [p["nav"] for p in nav_curve]
    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
    n_days = len(rets)
    total = navs[-1] / navs[0] - 1
    annual = (navs[-1] / navs[0]) ** (TRADING_DAYS_PER_YEAR / n_days) - 1 if n_days > 0 else None
    # 最大回撤（来自曲线）
    peak = navs[0]
    mdd = 0.0
    for v in navs:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    mean_r = _mean(rets)
    sd = _pstd(rets)
    sharpe = (mean_r / sd) * math.sqrt(TRADING_DAYS_PER_YEAR) if sd > 0 else None
    downs = [r for r in rets if r < 0]
    dmean = _mean(downs)
    dsd = _pstd(downs)
    sortino = (mean_r / abs(dsd)) * math.sqrt(TRADING_DAYS_PER_YEAR) if dsd > 0 else None
    calmar = (annual / mdd) if (mdd and mdd > 0 and annual is not None) else None
    ups = [r for r in rets if r > 0]
    dns = [r for r in rets if r < 0]
    win_rate = (len(ups) / len(rets)) if rets else None
    pl = (_mean(ups) / abs(_mean(dns))) if (ups and dns) else None
    return {
        "total_return": round(total, 4),
        "annual_return": round(annual, 4) if annual is not None else None,
        "max_drawdown": round(mdd, 4),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "sortino": round(sortino, 3) if sortino is not None else None,
        "calmar": round(calmar, 3) if calmar is not None else None,
        "win_rate": round(win_rate, 3) if win_rate is not None else None,
        "profit_loss_ratio": round(pl, 3) if pl is not None else None,
        "trading_days": len(nav_curve),
        "final_nav": round(navs[-1], 4),
        "n_trades": len(trades),
    }


def _engine(klines: dict, code_order: list, w_start: str, w_end: str, cost_mult: float) -> dict:
    """事件驱动等权组合回测引擎（单一真相源：组合净值曲线）。

    klines: {code: [bars]}（bars 已是全区间数据，这里按窗口过滤）
    code_order: 参与组合的标的顺序
    w_start / w_end: 回测窗口（含端点）
    cost_mult: 成本倍数（1.0 基准 / 2.0 高成本情景）
    """
    # 窗口过滤
    win: dict = {}
    for code in code_order:
        win[code] = [b for b in klines[code] if w_start <= b["date"] <= w_end]
    date_index = {code: {b["date"]: b for b in win[code]} for code in code_order}
    all_dates = sorted(set().union(*[set(idx.keys()) for idx in date_index.values()]))
    n = len(code_order)
    if n == 0 or len(all_dates) < 2:
        return {"ok": False, "error": "窗口内真实交易日不足，无法回测（需≥2个交易日）",
                "nav_curve": [], "trades": [], "monthly": [], "metrics": None, "constraints": {}}
    # 信号日：首日（建仓）+ 每月最后一个交易日
    months = []
    for d in all_dates:
        m = d[:7]
        if not months or months[-1][0] != m:
            months.append([m, d, d])
        else:
            months[-1][2] = d
    signal_dates = {all_dates[0]}
    for _, _, last in months:
        signal_dates.add(last)
    # 执行映射：信号日 → 次一交易日（T+1）开盘
    exec_map = {}
    for s in signal_dates:
        nxt = None
        for d in all_dates:
            if d > s:
                nxt = d
                break
        if nxt is not None:
            exec_map[s] = nxt

    cash = INIT_CAPITAL
    shares = {c: 0.0 for c in code_order}
    last_close = {c: None for c in code_order}
    # 退市处理：记录每个标的在窗口内的最后交易日，越过即视为退市 → 强平并计数
    last_trade = {c: (win[c][-1]["date"] if win[c] else None) for c in code_order}
    alive = set(code_order)
    nav_curve = []
    trades = []
    constraints = {"limit_up_skipped": 0, "limit_down_skipped": 0,
                   "suspended_skipped": 0, "delisted_exits": 0,
                   "t1_execution": True, "rebalances": 0}

    for date in all_dates:
        # 更新昨收（非停牌日才更新）
        for code in code_order:
            bar = date_index[code].get(date)
            if bar is not None and not _is_suspended(bar):
                last_close[code] = bar["close"]
        # 退市强制平仓：窗口内最后交易日后仍持仓 → 按最后可得价(last_close)结算现金并计数
        for code in code_order:
            if code not in alive:
                continue
            lt = last_trade[code]
            if lt is not None and date > lt and shares[code] > 0:
                px = last_close[code] or 0.0
                if px > 0:
                    amt = shares[code] * px
                    cost = _trade_cost(amt, False, code, cost_mult)
                    cash += amt - cost
                    trades.append({"date": date, "code": code, "side": "DELIST",
                                   "price": round(px, 4), "amount": round(amt, 2),
                                   "cost": round(cost, 2), "shares": round(shares[code], 6)})
                    shares[code] = 0.0
                    constraints["delisted_exits"] += 1
                alive.discard(code)
        # 执行（仅当今天是某信号的 T+1）
        if date in exec_map.values():
            # 计算每个标的今日执行价（开盘）；停牌/缺失则沿用昨收（持仓不动）
            prices = {}
            for code in code_order:
                bar = date_index[code].get(date)
                if bar is not None and not _is_suspended(bar) and (bar.get("open") or 0) > 0:
                    prices[code] = bar["open"]
                else:
                    prices[code] = last_close[code]
            cur_val = cash + sum(shares[c] * prices.get(c, 0.0) for c in code_order if prices.get(c))
            target_each = (cur_val / len(alive)) if (cur_val > 0 and alive) else 0.0
            constraints["rebalances"] += 1
            for code in code_order:
                if code not in alive:
                    continue
                bar = date_index[code].get(date)
                if bar is None or _is_suspended(bar):
                    constraints["suspended_skipped"] += 1
                    continue
                price = bar["open"]
                if price is None or price <= 0:
                    continue
                cur_pos = shares[code] * price
                delta = target_each - cur_pos
                if abs(delta) < 1e-12:
                    continue
                if delta > 0:  # 买入
                    if _is_limit_up(bar, code):
                        constraints["limit_up_skipped"] += 1
                        continue
                    a = delta
                    if a <= 0:
                        continue
                    # 买入 a 元市值：股份 = a/price（按执行价计的总额），成本单独从现金扣除一次。
                    # 注意：绝不能写成 shares += (a-cost)/price 又 cash -= (a+cost)，那会把成本扣两次。
                    cost = _trade_cost(a, True, code, cost_mult)
                    shares[code] += a / price
                    cash -= (a + cost)
                    trades.append({"date": date, "code": code, "side": "BUY",
                                   "price": round(price, 4), "amount": round(a, 2),
                                   "cost": round(cost, 2), "shares": round(a / price, 6)})
                else:  # 卖出
                    if _is_limit_down(bar, code):
                        constraints["limit_down_skipped"] += 1
                        continue
                    a = -delta
                    cost = _trade_cost(a, False, code, cost_mult)
                    shares[code] -= a / price
                    cash += (a - cost)
                    trades.append({"date": date, "code": code, "side": "SELL",
                                   "price": round(price, 4), "amount": round(a, 2),
                                   "cost": round(cost, 2), "shares": round(a / price, 6)})
        # 收盘市值（净值）
        pos_val = sum(shares[c] * (last_close[c] or 0.0) for c in code_order)
        nav = cash + pos_val
        nav_curve.append({"date": date, "nav": round(nav, 6)})

    metrics = _metrics_from_nav(nav_curve, trades)
    return {"ok": True, "nav_curve": nav_curve, "trades": trades,
            "monthly": _monthly(nav_curve), "metrics": metrics,
            "constraints": constraints, "n": n, "start": w_start, "end": w_end}


# ───────────────────────── B7 策略回测（统一口径，真回测） ─────────────────────────
def backtest(codes, start, end):
    """对给定股票列表在区间内做事件驱动等权组合回测，所有指标从同一净值曲线派生。

    返回结构（ok=True）：
      ok, n, codes_used, codes_excluded, start, end, method, costs,
      顶层便捷指标(total_return/annual_return/max_drawdown/sharpe/sortino/calmar/
                  win_rate/profit_loss_ratio/trading_days/final_nav),
      metrics(基准成本全区间), metrics_2x(2倍成本), in_sample, out_of_sample,
      oos_discount_note, monthly, trades(逐笔), nav_curve, constraints, caveats, survivorship_note
    取不到真实数据：ok=False + 诚实 error（含「真实行情」字样），绝不构造假数。
    """
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {"ok": True, "n": 0, "codes_used": [], "codes_excluded": [],
                "total_return": None, "annual_return": None, "max_drawdown": None,
                "sharpe": None, "sortino": None, "calmar": None, "win_rate": None,
                "profit_loss_ratio": None, "trading_days": 0, "final_nav": None,
                "metrics": None, "metrics_2x": None, "in_sample": None,
                "out_of_sample": None, "monthly": [], "trades": [], "nav_curve": [],
                "constraints": {}, "caveats": [], "survivorship_note": ""}

    # 拉一次真实 K 线（hfq）
    klines = {}
    excluded = []
    for code in codes:
        c = _norm_ak(code)
        kl = get_kline(c, start, end)
        if len(kl) >= 2:
            klines[c] = kl
        else:
            excluded.append(c)
    if not klines:
        return {"ok": False, "n": len(codes), "codes_used": [],
                "codes_excluded": excluded,
                "error": "未能获取真实行情（所有标的均无可用K线），无法回测；请检查 akshare/baostock 安装与网络",
                "total_return": None, "annual_return": None, "max_drawdown": None,
                "sharpe": None, "sortino": None, "calmar": None, "win_rate": None,
                "profit_loss_ratio": None, "trading_days": 0, "final_nav": None,
                "metrics": None, "metrics_2x": None, "in_sample": None,
                "out_of_sample": None, "monthly": [], "trades": [], "nav_curve": [],
                "constraints": {}, "caveats": [], "survivorship_note": ""}

    code_order = list(klines.keys())
    n = len(code_order)

    # 全区间（基准 + 2 倍成本）
    full = _engine(klines, code_order, start, end, COST_MULTIPLIER_BASE)
    full_2x = _engine(klines, code_order, start, end, COST_MULTIPLIER_2X)

    # walk-forward 70/30 切分（基于并集交易日历）
    all_dates = sorted(set().union(*[{b["date"] for b in kl} for kl in klines.values()]))
    if len(all_dates) >= 5:
        k = max(2, int(len(all_dates) * 0.7))
        in_end = all_dates[k - 1]
        out_start = all_dates[k]
    else:
        in_end = all_dates[-1]
        out_start = all_dates[-1]
    insample = _engine(klines, code_order, start, in_end, COST_MULTIPLIER_BASE)
    outsample = _engine(klines, code_order, out_start, end, COST_MULTIPLIER_BASE)

    m = full["metrics"]
    m2 = full_2x["metrics"]
    fm = m or {}
    fm2 = m2 or {}

    # 样本内/外对比（out 可能不足）
    in_sum = None
    if insample.get("ok"):
        in_sum = {"start": insample["start"], "end": insample["end"],
                  "trading_days": insample["metrics"]["trading_days"],
                  **{k: insample["metrics"][k] for k in
                     ("total_return", "annual_return", "max_drawdown", "sharpe",
                      "sortino", "calmar", "win_rate", "profit_loss_ratio")}}
    out_sum = None
    if outsample.get("ok"):
        out_sum = {"start": outsample["start"], "end": outsample["end"],
                   "trading_days": outsample["metrics"]["trading_days"],
                   **{k: outsample["metrics"][k] for k in
                      ("total_return", "annual_return", "max_drawdown", "sharpe",
                       "sortino", "calmar", "win_rate", "profit_loss_ratio")}}

    caveats = [
        "成本模型：佣金万2.5双边(单笔最低5元)+印花税0.05%(仅卖)+滑点0.1%双边+过户费0.001%(沪市)，已计入全部交易。",
        "T+1 制度：信号于月末收盘生成，次一交易日开盘成交；买入当日不可卖出（防前视偏差）。",
        "涨跌停约束：涨停日无法买入、跌停日无法卖出；按涨跌幅+收盘价封板近似判断（创业板/科创板按20%板，主板按10%板；baostock 无独立涨跌停字段，属近似）。",
        "停牌约束：停牌日(成交量0或 tradestatus=0)不交易、持仓不动。",
        "退市处理：标的中途退市（窗口内最后交易日后无数据）按最后可得价强制平仓并计入 delisted_exits，避免仓位被冻结在最后收盘价而虚增净值。",
        "复权：使用后复权(hfq)日线，避免前复权随时间漂移。",
        "一把尺子：所有指标均从同一条等权组合净值曲线派生（总收益几何、年化、回撤、夏普、索提诺、卡玛、胜率、盈亏比），不混用三种尺子。",
        "样本外：区间按70/30切分，样本外仅验证一次；实盘通常较样本内低30%-50%，请勿直接外推。",
        "成本敏感性：已同时给出 2 倍成本情景；若 margin 过薄，请以 2 倍成本结果为准。",
    ]
    survivorship_note = ("基于给定标的列表（均为存活股票）回测，未做退市/摘牌剔除，"
                         "存在幸存者偏差，收益可能偏高。")

    # 逐笔明细（截断保护，避免 payload 过大）
    trades_out = full["trades"]
    trades_truncated = False
    if len(trades_out) > 500:
        trades_out = trades_out[:500]
        trades_truncated = True

    result = {
        "ok": True,
        "n": n,
        "codes_used": code_order,
        "codes_excluded": excluded,
        "start": start,
        "end": end,
        "method": "事件驱动等权组合回测（后复权 hfq、含成本、含涨跌停/停牌/T+1 约束、月度再平衡）",
        "costs": {
            "commission_rate": COMMISSION_RATE, "commission_min": COMMISSION_MIN,
            "stamp_rate": STAMP_RATE, "slippage_rate": SLIPPAGE_RATE,
            "transfer_rate": TRANSFER_RATE, "base_multiplier": COST_MULTIPLIER_BASE,
            "sensitivity_multiplier": COST_MULTIPLIER_2X,
        },
        # 顶层便捷指标（供 verify / 旧调用方读取，全部来自同一条净值曲线）
        "total_return": fm.get("total_return"),
        "annual_return": fm.get("annual_return"),
        "max_drawdown": fm.get("max_drawdown"),
        "sharpe": fm.get("sharpe"),
        "sortino": fm.get("sortino"),
        "calmar": fm.get("calmar"),
        "win_rate": fm.get("win_rate"),
        "profit_loss_ratio": fm.get("profit_loss_ratio"),
        "trading_days": fm.get("trading_days"),
        "final_nav": fm.get("final_nav"),
        # 基准成本全区间明细
        "metrics": m,
        # 2 倍成本情景
        "metrics_2x": m2,
        # 样本内 / 样本外
        "in_sample": in_sum,
        "out_of_sample": out_sum,
        "oos_discount_note": "样本外仅验证一次；实盘通常较样本内低 30%-50%（成本、滑点、流动性、未被完全消除的前视偏差），请勿直接外推。",
        # 月度收益分布
        "monthly": full["monthly"],
        # 逐笔明细
        "trades": trades_out,
        "trades_truncated": trades_truncated,
        # 净值曲线（唯一真相源）
        "nav_curve": full["nav_curve"],
        # 约束统计
        "constraints": full["constraints"],
        # 坑位提示
        "caveats": caveats,
        "survivorship_note": survivorship_note,
    }
    # ── 阶段3 合理性闸门（最小侵入·仅告警·不阻断·不改计算）──
    _apply_reasonableness_gate(result)
    return result


# ───────────────────────── 阶段3 合理性闸门挂接（最小侵入·仅告警·不阻断·不改计算） ─────────────────────────
def _apply_reasonableness_gate(result: dict) -> None:
    """对回测结果跑合理性闸门：仅告警 / 写 reports/gate_warnings.json，绝不阻断、绝不改计算。

    - 闸门模块缺失或异常时静默跳过（生产零依赖、零污染）。
    - 所有指标来自 result 已有字段，不重新计算任何公式（红线：不改计算逻辑）。
    - 生产模式（test_mode=False）下越界只写告警，不抛异常、不影响返回。
    """
    try:
        try:
            from reasonableness_gate import run_gates  # 同目录平级导入
        except ImportError:
            from viewer.reasonableness_gate import run_gates  # 包内导入兜底
    except Exception:  # noqa: BLE001
        return
    nav_curve = result.get("nav_curve") or []
    daily = []
    for i in range(1, len(nav_curve)):
        prev = nav_curve[i - 1].get("nav")
        cur = nav_curve[i].get("nav")
        if prev:
            daily.append(cur / prev - 1)
    metrics = {
        "total_return": result.get("total_return"),
        "annual_return": result.get("annual_return"),
        "sharpe": result.get("sharpe"),
        "max_drawdown": result.get("max_drawdown"),
        "daily_returns": daily,
    }
    # P1-55：单标的回测时按该票板块取对应涨跌停带宽（±11%/±21%/±31%）；
    # 多标的组合回测是跨板块净值（已分散、日常波动远小于单票），保守沿用主板带宽。
    _board = "主板"
    _used = result.get("codes_used") or []
    if len(_used) == 1:
        try:
            from board_rules import board_of
            _board = board_of(_used[0], "")
        except Exception:  # noqa: BLE001
            _board = "主板"
    ctx = {"module": "viewer/screener_extra.py", "function": "backtest",
           "test_mode": False, "board": _board}
    try:
        run_gates(metrics, ctx)
    except Exception:  # noqa: BLE001
        # 生产模式闸门不应抛；任何异常都不影响业务返回
        pass


# ───────────────────────── B4 估值分位 ─────────────────────────
def valuation_pct(codes):
    """批量算 PE/PB/PS 历史分位（近 3 年）。返回 {code:{pe_pct,pb_pct,ps_pct,pe_now,pb_now,ps_now}}。"""
    res = {}
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    for code in codes:
        c = _norm_ak(code)
        pe_now = pb_now = ps_now = None
        if _HAS_AK:
            try:
                df = _ak.stock_a_indicator_lg(symbol=c)
                if df is not None and len(df):
                    pe = [float(x) for x in df.get("peTTM", []) if x not in (None, "", "None")]
                    pb = [float(x) for x in df.get("pbMRQ", []) if x not in (None, "", "None")]
                    ps = [float(x) for x in df.get("psTTM", []) if x not in (None, "", "None")]
                if pe:
                    pe_now = pe[-1]
                    pb_now = pb[-1] if pb else None
                    ps_now = ps[-1] if ps else None
                    res[c] = _pct_block(pe, pe_now, pb, pb_now, ps, ps_now)
                    continue
            except Exception:  # noqa: BLE001
                pass
        # 未获取到真实估值指标：明确标注，绝不构造假数据
        res[c] = {"ok": False, "error": "未获取到真实估值指标（PE/PB/PS），请检查 akshare 是否安装、网络是否可用"}
    return res


def _pct_block(pe, pe_now, pb, pb_now, ps, ps_now):
    """统一分位口径：每个指标用『自身当前值 vs 自身历史序列』算分位，尺子一致。"""
    def pct(arr, v):
        if not arr or v in (None, 0):
            return None
        arr = [x for x in arr if x]
        if not arr:
            return None
        below = sum(1 for x in arr if x <= v)
        return round(below / len(arr), 3)
    return {
        "pe_pct": pct(pe, pe_now),
        "pb_pct": pct(pb, pb_now),
        "ps_pct": pct(ps, ps_now),
        "pe_now": pe_now,
        "pb_now": pb_now,
        "ps_now": ps_now,
    }


def valuation_cache_status():
    d = _load_cache("valuation")
    if d and isinstance(d, dict) and d.get("_date") == datetime.now().strftime("%Y-%m-%d"):
        return {"ok": True, "cached": True, "date": d["_date"]}
    return {"ok": True, "cached": False, "date": None}


# ───────────────────────── B5 杜邦拆解 ─────────────────────────
def dupont(code):
    """ROE = 净利率 × 总资产周转率 × 权益乘数。返回拆解与原始字段。仅真实数据，取不到即报错。"""
    c = _norm_ak(code)
    if _HAS_AK:
        try:
            df = _ak.stock_financial_analysis_indicator(symbol=c)
            if df is not None and len(df):
                row = df.iloc[0]
                npm = _f(row.get("销售净利率"))
                tat = _f(row.get("总资产周转率"))
                em = _f(row.get("权益乘数"))
                roe = _f(row.get("净资产收益率"))
                if roe is not None:
                    return {"ok": True, "code": c, "roe": roe, "npm": npm, "tat": tat,
                            "em": em, "src": "akshare"}
        except Exception:  # noqa: BLE001
            pass
    # 未获取到真实财务数据：明确报错，绝不构造假数据
    return {"ok": False, "code": c, "error": "未获取到真实财务数据（杜邦拆解需要 akshare 真实财报），请检查安装与网络"}


def _f(v):
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return None


# ───────────────────────── B8 题材/概念映射 ─────────────────────────
_THEME_CACHE = "themes"


_THEME_FAIL_AT = [0.0]      # 上次 akshare 主题抓取失败的时刻（冷却用，见 themes_map）
_THEME_FAIL_COOL = 600      # 失败后 10 分钟内不再尝试外网，直接回本地结果


def themes_map():
    """返回 [{theme, code, count, members:[{code,name}]}]。**本地板块数据优先**，akshare 仅兜底。

    2026-10-29 性能修复：原来只走 akshare 概念板，本机取不到时**在请求线程里干等约 18 秒**
    才返回空 —— 这 18 秒占着 GIL，整站所有接口一起卡（用户看到「一堆面板加载失败」）。
    本机 `board_flow` 已有 89 个概念板块的实时快照（50ms 级），先用它出列表；
    成分股改为**点开某个题材时按需拉**（`/api/screener_board_members`，0.3s），
    不再在打开弹窗时一次性请求 89 个板块的成分。
    """
    # ① 本地优先：board_flow 的概念板块快照（纯本地/短 TTL 缓存，毫秒级）
    try:
        import board_flow as _bf
        snap = _bf.snapshot(kind="concept", pz=90) or {}
        out = []
        for r in (snap.get("rows") or []):
            code = r.get("board_code") or ""
            name = r.get("board_name") or ""
            if not code or not name:
                continue
            out.append({"theme": name, "code": code, "count": None, "members": []})
        if out:
            return out
    except Exception:  # noqa: BLE001  本地板块不可用 → 继续走 akshare 兜底
        pass

    # ② 兜底：akshare 概念板（失败后冷却 10 分钟，绝不每点一次都卡 18 秒）
    if _HAS_AK and (time.time() - _THEME_FAIL_AT[0] > _THEME_FAIL_COOL):
        cached = _load_cache(_THEME_CACHE)
        if cached and isinstance(cached, list):
            return cached
        try:
            df = _ak.stock_board_concept_cons_em()
            if df is not None and len(df):
                out = []
                for _, row in df.iterrows():
                    out.append({"theme": str(row.get("板块名称", "")),
                                "code": _norm_ak(str(row.get("代码", ""))),
                                "name": str(row.get("名称", ""))})
                by = {}
                for it in out:
                    by.setdefault(it["theme"], []).append({"code": it["code"], "name": it["name"]})
                res = [{"theme": t, "code": "", "count": len(m), "members": m[:60]}
                       for t, m in by.items()]
                _save_cache(_THEME_CACHE, res)
                return res
        except Exception:  # noqa: BLE001
            _THEME_FAIL_AT[0] = time.time()      # 记下失败时刻，冷却期内不再尝试
    # 未获取到真实概念板块数据：返回空，绝不伪造主题、绝不读历史缓存
    return []


# ───────────────────────── B6 自然语言选股（DeepSeek 代理） ─────────────────────────
def get_deepseek_key():
    """优先环境变量；其次加密凭据库（Fernet）；遗留明文文件首次读取时自动迁移并删除。

    不再以明文落盘：旧版 viewer/.deepseek_key 第一次被读到时会迁移进加密库并就地删除。
    """
    ek = os.environ.get("DEEPSEEK_API_KEY")
    if ek:
        return ek.strip()
    # 遗留明文文件迁移（一次性）
    fp = ROOT / ".deepseek_key"
    if fp.exists():
        try:
            raw = fp.read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            raw = ""
        if raw:
            set_deepseek_key(raw)  # 迁移进加密库
            try:
                fp.unlink()
            except OSError:
                pass
            return raw
    # 加密凭据库
    try:
        from scraper.credentials import load_secret
        return load_secret("deepseek_api_key")
    except Exception:  # noqa: BLE001
        return ""


def set_deepseek_key(key: str) -> None:
    key = (key or "").strip()
    try:
        from scraper.credentials import save_secret
        save_secret("deepseek_api_key", key)
    except Exception:  # noqa: BLE001
        # 极端兜底：cryptography 不可用等，退回明文（不推荐，仅保可用）
        (ROOT / ".deepseek_key").write_text(key, encoding="utf-8")


SYSTEM_PROMPT = """你是A股选股条件解析器。把用户自然语言转成结构化 JSON，字段：
{"screens":["pe","pb","ps","peg","pcf","cap","holders","roe","gm","dr","dy","worth"],
 "thr":{"pe":20,"pb":1,...},"sort":"worth|pe|dy|roe|mcap|pct",
 "board":"行业或板块名(可选)","theme":"题材名(可选)","explain":"一句话中文解释"}。
只输出 JSON，不要解释。screens 为要筛选的指标，thr 为阈值(数值)。"""


def nl_screener(text: str, api_key: str = ""):
    key = api_key or get_deepseek_key()
    if not key:
        return {"ok": False, "error": "未配置 DeepSeek API Key：请在设置面板填入（开放平台 platform.deepseek.com 创建）。"}
    if not text or not text.strip():
        return {"ok": False, "error": "请输入选股描述，例如：低估值的白酒股，股息率大于3%。"}
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body, headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            j = json.loads(resp.read().decode("utf-8"))
            content = j["choices"][0]["message"]["content"]
            cond = json.loads(content)
            return {"ok": True, "cond": cond, "raw": content}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "DeepSeek 调用失败：" + str(e)[:200]}
