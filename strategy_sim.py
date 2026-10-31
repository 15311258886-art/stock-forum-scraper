# -*- coding: utf-8 -*-
"""策略回测引擎：用本地已抓取的 A 股日线，对 strategy_data.STRATEGIES 里的公开策略
做历史模拟，结果落库 viewer/app.db 的新表 strategy_sim_window / strategy_sim_meta
（供「策略回测」板块秒读）。

2026-09-26 升级（v2）：14 条 → 100 条策略、单窗口 → 三窗口、同日成交 → 次日成交、
新增一字板可成交性过滤。核心改动见 docs/策略回测板块_技术设计_v1.0.md。

口径（重要，界面上会一并标注）：
- 数据源：新浪日线（不复权），本地缓存 kline_cache.json，避免重复请求。
- 标的池：全市场流动性排序（`stock_liquidity` → 本地日线近 60 日均额 → 干净宇宙），
  默认取前 N 只，可按 --limit 调整。**2026-10-30 D35 起不再用 lhb_stocks 兜底**（见 `universe()`）。
- 交易假设：单标的、等权、满仓进出、**信号当日收盘判定 → 次日收盘成交**；
  **不计手续费与滑点**。这是"策略相对强弱"的横向比较，不是可实现收益的精确预测。
- 可成交性：次日若为一字板（开=高=低=收且涨跌幅接近涨跌停），视为买不到 → 撤单跳过本笔；
  涨跌停阈值按板块取（主板 10% / 创业板·科创板 20% / 北交所 30%，名称含 ST 则 5%）。
- 基准：上证指数（000001）同期买入持有。
- 输出：strategy_sim_window（策略 × 窗口）+ strategy_sim_meta（全局口径）两表；
  旧 strategy_sim 表保留、不再写入。另有 strategy_sim_result.json 供人工核对。

用法：
    python strategy_sim.py --limit 160 --bars 520
    python strategy_sim.py --limit 20 --bars 520 --targets ma_cross_20_60,break_20d_high
"""
from __future__ import annotations

import argparse
import json
import math  # P1-21 BH-FDR 二项 p 值（零外部依赖）
import os
import sqlite3
import ssl
import sys
import time
import urllib.request
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "viewer", "app.db")
KLINE_DB = os.path.join(BASE, "data", "kline_daily.db")   # 本地日线全库（1700 万行，后复权）
OUT = os.path.join(BASE, "strategy_sim_result.json")
KCACHE = os.path.join(BASE, "kline_cache.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
import netctx  # noqa: E402  —— 统一出网 TLS 口径（整改 #27）
_CTX = netctx.https_context("money.finance.sina.com.cn")  # 整改 #27：默认不校验，按环境变量可开启
# 空代理 opener：绕过本机可能存在的代理劫持（务必保留）。
_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                 urllib.request.HTTPSHandler(context=_CTX))

GAP = 0.18          # 新浪节流
MIN_TRADES = 30     # 全池交易次数低于此值 → 该窗口标注"样本不足"

# 2026-10-15 P0-2 / DEF-006 修复：复权漂移。
# 旧实现抓「不复权」日线，多年回测里分红/送转会让价格出现人为跳空，
# 收益率被复权方式扭曲（同一策略用不复权 vs 后复权结果能差十几个点）。
# 现默认抓「后复权(hfq)」日线——除权除息后价格连续、收益率口径自洽。
# 旧缓存是「不复权」根，必须按 adj 标记区分，否则新旧混用会把漂移带回来。
ADJ = "hfq"         # 回测统一用后复权；"" 才是不复权（仅调试用）
ADJ_CACHE_OK = (ADJ or "")  # 缓存里只复用与当前 ADJ 一致的批次

# 2026-10-15 P0-2 / DEF-007 修复：过拟合（walk-forward 样本外验证）。
# 旧实现只报「全样本」收益，等于同时吃了训练集，数字必然虚高（策略参数本就按全段调）。
# 现每个窗口按时间序切 70% 训练 / 30% 验证：样本外只验一次，如实对比两段，
# 样本外明显转负/微弱 → 标记「疑似过拟合」，界面给灰度角标而非盲目「已验证」。
WF_IN_SAMPLE_FRAC = 0.70   # 前 70% 训练段
OOS_MIN_TRADES = 15        # 样本外至少这么多笔交易，验证结论才有统计意义

# ---- 2026-09-26 新增：抓取长度 / 三窗口 / 板块涨跌停阈值 ----------------------
FETCH_BARS = 520                       # 一次抓取根数（指标在整段上算，窗口只截统计段）
# 主参考窗口（王总 2026-10-06 定）：「以近两年（一年半~两年）的涨跌为主要参考，太早的不参考」。
# 于是 mid 不再写死 500 根（那是 2 年零 1 个月，多出来的一段会拖进 2024 年夏的行情），
# 改成按年交易日折算成整两年；评分 / 排名 / 评级一律只认 mid，y1/h1 仅作辅助对照。
LOOKBACK_YEARS = 2.0                   # 主参考窗口长度（年）
TRADING_DAYS_PER_YEAR = 244
MAIN_WINDOW_LABEL = "mid"
WINDOWS = {"mid": int(LOOKBACK_YEARS * TRADING_DAYS_PER_YEAR),   # 近两年 = 主参考
           "y1": 250,                                            # 近一年（辅助）
           "h1": 125}                                           # 近半年（辅助）
BOARD_LIMIT = {"沪市主板": 0.10, "深市主板": 0.10,
               "创业板": 0.20, "科创板": 0.20, "北交所": 0.30}
ST_LIMIT = 0.05                        # 名称含 ST / *ST 覆盖为 5%

# 板块判定复用 viewer/stock_names.py（不新造）；导入失败时退回本地等价实现。
sys.path.insert(0, BASE)
try:
    from viewer.stock_names import board_of  # noqa: E402
except Exception:  # noqa: BLE001
    def board_of(code: str) -> str:
        for pre, nm in (("60", "沪市主板"), ("68", "科创板"), ("00", "深市主板"),
                        ("30", "创业板"), ("43", "北交所"), ("83", "北交所"),
                        ("87", "北交所"), ("92", "北交所")):
            if (code or "").startswith(pre):
                return nm
        return "未知板块"


# ---------------------------------------------------------------- 数据获取
def fetch_kline(code: str, datalen: int, adj: str = ADJ) -> list[dict]:
    """新浪日线。code 为 6 位数字。

    2026-10-15 P0-2 / DEF-006：默认抓**后复权(hfq)**，保证多年回测收益率口径自洽
    （不复权会因分红/送转出现人为跳空，扭曲策略收益）。adj='' 表示不复权（仅调试）。
    """
    pre = "sh" if code[0] in "659" else ("bj" if code[0] in "48" else "sz")
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={pre}{code}&scale=240&ma=no&datalen={datalen}"
           + (f"&adj={adj}" if adj else ""))
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                              "Referer": "https://finance.sina.com.cn/"})
    with _OP.open(req, timeout=20) as r:
        raw = r.read().decode("utf-8", "ignore")
    data = json.loads(raw)
    rows = []
    for x in data or []:
        try:
            rows.append({"d": str(x["day"])[:10], "o": float(x["open"]), "h": float(x["high"]),
                         "l": float(x["low"]), "c": float(x["close"]),
                         "v": float(x.get("volume") or 0)})
        except (TypeError, ValueError, KeyError):
            continue
    return rows


def load_kcache() -> dict:
    if os.path.exists(KCACHE):
        try:
            return json.load(open(KCACHE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_kcache(c: dict) -> None:
    try:
        json.dump(c, open(KCACHE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        print("  [warn] 缓存写入失败:", str(e)[:80], flush=True)


# ---------------------------------------------------------------- 基础指标
def sma(x: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x: list[float], n: int) -> list[float]:
    k = 2.0 / (n + 1)
    out = []
    prev = x[0] if x else 0.0
    for v in x:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(c: list[float], n: int = 6) -> list[float | None]:
    out: list[float | None] = [None] * len(c)
    if len(c) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    ag, al = gain / n, loss / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(c: list[float], fast: int = 12, slow: int = 26, sig: int = 9):
    ef, es = ema(c, fast), ema(c, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, sig)
    return dif, dea


def kdj(h, l, c, n: int = 9):
    ks: list[float | None] = [None] * len(c)
    ds: list[float | None] = [None] * len(c)
    k = d = 50.0
    for i in range(len(c)):
        if i < n - 1:
            continue
        hh = max(h[i - n + 1:i + 1])
        ll = min(l[i - n + 1:i + 1])
        rsv = 50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100
        k = (2 * k + rsv) / 3
        d = (2 * d + k) / 3
        ks[i], ds[i] = k, d
    return ks, ds


def boll(c: list[float], n: int = 20, k: float = 2.0):
    mid = sma(c, n)
    up: list[float | None] = [None] * len(c)
    dn: list[float | None] = [None] * len(c)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        m = mid[i] or 0.0
        var = sum((x - m) ** 2 for x in w) / n
        sd = var ** 0.5
        up[i], dn[i] = m + k * sd, m - k * sd
    return mid, up, dn


def rolling_max(x: list[float], n: int) -> list[float | None]:
    return [max(x[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(x))]


def rolling_min(x: list[float], n: int) -> list[float | None]:
    return [min(x[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(x))]


# ---- 2026-09-26 新增指标（全部纯标准库，只用 OHLCV） --------------------------
def atr(h, l, c, n: int = 14) -> list[float | None]:
    """Wilder ATR：真实波幅的指数平滑均值。"""
    out: list[float | None] = [None] * len(c)
    if len(c) == 0:
        return out
    trs = []
    for i in range(len(c)):
        if i == 0:
            trs.append(h[i] - l[i])
        else:
            trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    if len(c) < n:
        return out
    a = sum(trs[:n]) / n
    out[n - 1] = a
    for i in range(n, len(c)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def cci(h, l, c, n: int = 14) -> list[float | None]:
    """Commodity Channel Index。"""
    out: list[float | None] = [None] * len(c)
    tp = [(h[i] + l[i] + c[i]) / 3.0 for i in range(len(c))]
    ma = sma(tp, n)
    for i in range(n - 1, len(c)):
        m = ma[i]
        if m is None:
            continue
        w = tp[i - n + 1:i + 1]
        md = sum(abs(x - m) for x in w) / n
        out[i] = 0.0 if md == 0 else (tp[i] - m) / (0.015 * md)
    return out


def williams_r(h, l, c, n: int = 14) -> list[float | None]:
    """Williams %R，取值区间 [-100, 0]。"""
    out: list[float | None] = [None] * len(c)
    for i in range(n - 1, len(c)):
        hh = max(h[i - n + 1:i + 1])
        ll = min(l[i - n + 1:i + 1])
        out[i] = -50.0 if hh == ll else (hh - c[i]) / (hh - ll) * -100.0
    return out


def dmi_adx(h, l, c, n: int = 14):
    """DMI / ADX（Wilder）。返回 (+DI, -DI, ADX) 三条与 c 等长的序列。"""
    N = len(c)
    pdi: list[float | None] = [None] * N
    mdi: list[float | None] = [None] * N
    adx: list[float | None] = [None] * N
    if N < n + 1:
        return pdi, mdi, adx
    tr = [0.0] * N
    pdm = [0.0] * N
    mdm = [0.0] * N
    for i in range(1, N):
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr_ = sum(tr[1:n + 1])
    ap = sum(pdm[1:n + 1])
    am = sum(mdm[1:n + 1])
    dxs = []
    for i in range(n, N):
        if i > n:
            atr_ = atr_ - atr_ / n + tr[i]
            ap = ap - ap / n + pdm[i]
            am = am - am / n + mdm[i]
        p = 100.0 * ap / atr_ if atr_ else 0.0
        m = 100.0 * am / atr_ if atr_ else 0.0
        pdi[i], mdi[i] = p, m
        dxs.append((i, 100.0 * abs(p - m) / (p + m) if (p + m) else 0.0))
    if len(dxs) >= n:
        adx_val = sum(d for _, d in dxs[:n]) / n
        adx[dxs[n - 1][0]] = adx_val
        for i, dx in dxs[n:]:
            adx_val = (adx_val * (n - 1) + dx) / n
            adx[i] = adx_val
    return pdi, mdi, adx


def obv(c, v) -> list[float]:
    """能量潮：涨日累加成交量、跌日累减、平盘不变。"""
    out = [0.0] * len(c)
    for i in range(1, len(c)):
        if c[i] > c[i - 1]:
            out[i] = out[i - 1] + v[i]
        elif c[i] < c[i - 1]:
            out[i] = out[i - 1] - v[i]
        else:
            out[i] = out[i - 1]
    return out


def vol_ratio(v, n: int = 5) -> list[float | None]:
    """量比近似：当日量 / 前 n 日均量。"""
    out: list[float | None] = [None] * len(v)
    for i in range(n, len(v)):
        base = sum(v[i - n:i]) / n
        out[i] = (v[i] / base) if base > 0 else None
    return out


def nr7(h, l, n: int = 7) -> list[bool]:
    """NR7：当日振幅为最近 n 日最小。"""
    out = [False] * len(h)
    for i in range(n - 1, len(h)):
        rng = [h[j] - l[j] for j in range(i - n + 1, i + 1)]
        out[i] = (h[i] - l[i]) <= min(rng)
    return out


def zscore(c, n: int = 20) -> list[float | None]:
    """价格相对 n 日均值的标准差倍数。"""
    out: list[float | None] = [None] * len(c)
    ma = sma(c, n)
    for i in range(n - 1, len(c)):
        m = ma[i]
        if m is None:
            continue
        w = c[i - n + 1:i + 1]
        var = sum((x - m) ** 2 for x in w) / n
        sd = var ** 0.5
        out[i] = None if sd == 0 else (c[i] - m) / sd
    return out


def bias(c, n: int) -> list[float | None]:
    """乖离率：(收盘 - MA) / MA * 100。"""
    out: list[float | None] = [None] * len(c)
    ma = sma(c, n)
    for i in range(n - 1, len(c)):
        if ma[i]:
            out[i] = (c[i] - ma[i]) / ma[i] * 100.0
    return out


def boll_width(c, n: int = 20, k: float = 2.0) -> list[float | None]:
    """布林带宽：(上轨 - 下轨) / 中轨 * 100。"""
    mid, up, dn = boll(c, n, k)
    out: list[float | None] = [None] * len(c)
    for i in range(len(c)):
        if mid[i] and up[i] is not None and dn[i] is not None:
            out[i] = (up[i] - dn[i]) / mid[i] * 100.0
    return out


def _realized_vol(series: list[float], n: int = 20) -> list[float | None]:
    """n 日年化波动率（%），用日收益标准差近似，供波动率类策略复用。"""
    L = len(series)
    out: list[float | None] = [None] * L
    ret = [0.0] * L
    for i in range(1, L):
        ret[i] = (series[i] / series[i - 1] - 1.0) if series[i - 1] else 0.0
    for i in range(n, L):
        w = ret[i - n + 1:i + 1]
        m = sum(w) / len(w)
        sd = (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5
        out[i] = sd * (244 ** 0.5) * 100.0
    return out


def _align(series, n: int, fill: float = 0.0) -> list[float]:
    """把基准/指数序列按尾部对齐到个股长度 n（不足则用首值前补）。"""
    if not series:
        return [fill] * n
    if len(series) >= n:
        return list(series[-n:])
    return [series[0]] * (n - len(series)) + list(series)


# ---------------------------------------------------------------- 板块与可成交性
def limit_pct(code: str, name: str = "") -> float:
    """按板块 + 时间给出涨跌停阈值（统一走 board_rules 唯一真源，P1-07）。

    北交所 30% / 创业板·科创板 20% / 主板 10% / ST 5%；创业板注册制前(2019-08-24) 10%。
    """
    try:
        from board_rules import limit_pct as _br_limit_pct
        return _br_limit_pct(code, name)
    except Exception:  # noqa: BLE001
        if name and "ST" in str(name).upper():
            return ST_LIMIT
        return BOARD_LIMIT.get(board_of(code), 0.10)


def executable(o, h, l, c, prev_c, code: str = "", name: str = "") -> bool:
    """次日是否可成交：一字板（开=高=低=收 且 涨跌幅接近涨跌停）判定为买不到/卖不掉。

    只砍"一字板"；开盘价可成交的换手板一律保留。
    """
    if prev_c is None or prev_c <= 0:
        return True
    lim = limit_pct(code, name)
    one_word = (o == h == l == c)
    near = abs(c / prev_c - 1.0) >= lim * 0.97
    return not (one_word and near)


# ---------------------------------------------------------------- 信号
def build_signals(key: str, k: list[dict], ctx: dict | None = None,
                  code: str = "", name: str = ""):
    """返回 (buy, sell) 两个与 k 等长的 bool 列表。

    严禁未来函数：buy[i] / sell[i] 只能用到 i 日及之前的数据。
    90 条可回测策略按 9 个流派分组实现；ctx 提供指数/市场宽度（择时类需要）。
    """
    n = len(k)
    o = [r["o"] for r in k]
    c = [r["c"] for r in k]
    h = [r["h"] for r in k]
    l = [r["l"] for r in k]
    v = [r["v"] for r in k]
    buy = [False] * n
    sell = [False] * n

    def cross(a, b, i, up=True):
        if i < 1 or a[i] is None or b[i] is None or a[i - 1] is None or b[i - 1] is None:
            return False
        return (a[i - 1] <= b[i - 1] and a[i] > b[i]) if up else (a[i - 1] >= b[i - 1] and a[i] < b[i])

    # ============ 口径受限 4 条：不与「等权满仓」相容 → 显式全 False ============
    if key in ("vol_target_sizing", "kelly_fraction_sizing", "dual_momentum", "monthly_rebalance"):
        return buy, sell

    # ================= 趋势跟踪（13 条） =================
    if key in ("ma_cross_20_60", "ma_cross_5_20", "ma_cross_10_30", "ma_cross_10_60"):
        f, s = {"ma_cross_20_60": (20, 60), "ma_cross_5_20": (5, 20),
                "ma_cross_10_30": (10, 30), "ma_cross_10_60": (10, 60)}[key]
        maf, mas = sma(c, f), sma(c, s)
        for i in range(1, n):
            if cross(maf, mas, i, True):
                buy[i] = True
            if cross(maf, mas, i, False):
                sell[i] = True

    elif key == "ma_cross_second_gold":
        ma20, ma60 = sma(c, 20), sma(c, 60)
        last_death = -10 ** 9
        for i in range(1, n):
            if cross(ma20, ma60, i, False):
                last_death = i
                sell[i] = True
            if cross(ma20, ma60, i, True) and (i - last_death) <= 60:
                buy[i] = True

    elif key in ("ema_cross_12_26", "ema_cross_20_50"):
        f, s = (12, 26) if key == "ema_cross_12_26" else (20, 50)
        ef, es = ema(c, f), ema(c, s)
        for i in range(1, n):
            if cross(ef, es, i, True):
                buy[i] = True
            if cross(ef, es, i, False):
                sell[i] = True

    elif key == "ma_bull_align":
        m5, m20, m60 = sma(c, 5), sma(c, 20), sma(c, 60)
        for i in range(1, n):
            a, b, d = m5[i], m20[i], m60[i]
            if a and b and d and a > b > d and c[i] > a and c[i - 1] <= (m5[i - 1] or 0):
                buy[i] = True
            if b and c[i] < b:
                sell[i] = True

    elif key == "ma_bull_align_strict":
        m5, m10, m20, m60 = sma(c, 5), sma(c, 10), sma(c, 20), sma(c, 60)
        for i in range(1, n):
            a, b, d, e = m5[i], m10[i], m20[i], m60[i]
            if a and b and d and e and a > b > d > e and c[i] > a:
                buy[i] = True
            if d and c[i] < d:
                sell[i] = True

    elif key in ("pullback_ma20", "pullback_ma10", "pullback_ma60"):
        p, touch, stop = {"pullback_ma20": (20, 0.02, 0.03), "pullback_ma10": (10, 0.02, 0.03),
                          "pullback_ma60": (60, 0.03, 0.04)}[key]
        ma = sma(c, p)
        for i in range(1, n):
            m, mp = ma[i], ma[i - 1]
            if m and mp and m > mp and l[i] <= m * (1 + touch) and c[i] > m:
                buy[i] = True
            if m and c[i] < m * (1 - stop):
                sell[i] = True

    elif key == "price_above_ma250":
        ma250 = sma(c, 250)
        for i in range(1, n):
            m = ma250[i]
            mp = ma250[i - 20] if i >= 20 else None
            if m and mp and c[i] > m and m >= mp:
                buy[i] = True
            if m and c[i] < m:
                sell[i] = True

    # ================= 均值回归（11 条） =================
    elif key == "rsi_reversal":
        r = rsi(c, 6)
        held = 0
        for i in range(1, n):
            if r[i] is None or r[i - 1] is None:
                continue
            if r[i - 1] < 20 <= r[i]:
                buy[i] = True
                held = 0
            if r[i] > 70:
                sell[i] = True
            held += 1
            if held >= 20:
                sell[i] = True
                held = 0

    elif key == "rsi2_extreme":
        r = rsi(c, 2)
        ma5 = sma(c, 5)
        held = 0
        for i in range(1, n):
            if r[i] is None:
                continue
            if r[i] < 10:
                buy[i] = True
                held = 0
            else:
                held += 1
            if held >= 5 or (ma5[i] and c[i] > ma5[i]):
                if held > 0:
                    sell[i] = True

    elif key == "boll_lower":
        mid, up, dn = boll(c)
        touched = False
        for i in range(1, n):
            if dn[i] is None:
                continue
            if c[i] < dn[i]:
                touched = True
            elif touched and c[i] > dn[i]:
                buy[i] = True
                touched = False
            if mid[i] and c[i] >= mid[i]:
                sell[i] = True

    elif key == "boll_lower_double":
        mid, up, dn = boll(c)
        for i in range(1, n):
            if dn[i] is None or dn[i - 1] is None:
                continue
            if c[i] < dn[i] and c[i - 1] < dn[i - 1]:
                buy[i] = True
            if mid[i] and c[i] >= mid[i]:
                sell[i] = True

    elif key in ("mean_revert_ma20", "mean_revert_ma60"):
        p, dev = (20, 0.12) if key == "mean_revert_ma20" else (60, 0.18)
        ma, ma20 = sma(c, p), sma(c, 20)
        for i in range(1, n):
            if ma[i] and c[i] < ma[i] * (1 - dev):
                buy[i] = True
            if key == "mean_revert_ma20":
                if ma[i] and c[i] > ma[i]:
                    sell[i] = True
            else:
                if ma20[i] and c[i] > ma20[i]:
                    sell[i] = True

    elif key == "zscore_revert_20":
        z = zscore(c, 20)
        for i in range(1, n):
            if z[i] is None:
                continue
            if z[i] < -2:
                buy[i] = True
            if z[i] > 0:
                sell[i] = True

    elif key == "cci_oversold":
        cc = cci(h, l, c, 14)
        for i in range(1, n):
            if cc[i] is None or cc[i - 1] is None:
                continue
            if cc[i - 1] <= -100 < cc[i]:
                buy[i] = True
            if cc[i] > 100:
                sell[i] = True

    elif key == "williams_oversold":
        wr = williams_r(h, l, c, 14)
        for i in range(1, n):
            if wr[i] is None or wr[i - 1] is None:
                continue
            if wr[i - 1] <= -80 < wr[i]:
                buy[i] = True
            if wr[i - 1] >= -20 > wr[i]:
                sell[i] = True

    elif key == "bias_revert_6":
        b = bias(c, 6)
        for i in range(1, n):
            if b[i] is None:
                continue
            if b[i] < -8:
                buy[i] = True
            if b[i] > 0:
                sell[i] = True

    elif key == "consecutive_down_4":
        ma5 = sma(c, 5)
        held = 0
        for i in range(4, n):
            if c[i] < o[i] and c[i - 1] < o[i - 1] and c[i - 2] < o[i - 2] and c[i - 3] < o[i - 3]:
                buy[i] = True
                held = 0
            else:
                held += 1
            if held >= 5 or (ma5[i] and c[i] > ma5[i]):
                if held > 0:
                    sell[i] = True

    # ================= 突破（11 条） =================
    elif key in ("break_10d_high", "break_20d_high", "break_60d_high", "break_120d_high",
                 "turtle_donchian_20_10", "turtle_donchian_55_20"):
        e, x = {"break_10d_high": (10, 5), "break_20d_high": (20, 10),
                "break_60d_high": (60, 20), "break_120d_high": (120, 20),
                "turtle_donchian_20_10": (20, 10), "turtle_donchian_55_20": (55, 20)}[key]
        hh, ll = rolling_max(c, e), rolling_min(c, x)
        for i in range(1, n):
            if hh[i - 1] is not None and c[i] > hh[i - 1]:
                buy[i] = True
            if ll[i - 1] is not None and c[i] < ll[i - 1]:
                sell[i] = True

    elif key == "box_breakout":
        hh20, ll20, ll10 = rolling_max(c, 20), rolling_min(c, 20), rolling_min(c, 10)
        for i in range(1, n):
            if hh20[i - 1] is not None and ll20[i - 1]:
                if (hh20[i - 1] - ll20[i - 1]) / ll20[i - 1] < 0.12 and c[i] > hh20[i - 1]:
                    buy[i] = True
            if ll10[i - 1] is not None and c[i] < ll10[i - 1]:
                sell[i] = True

    elif key == "box_breakout_retest":
        hh20, ll10 = rolling_max(c, 20), rolling_min(c, 10)
        broke_at, broke_level = -10 ** 9, 0.0
        for i in range(1, n):
            if hh20[i - 1] is not None and c[i] > hh20[i - 1]:
                broke_at, broke_level = i, hh20[i - 1]
            if broke_at > 0 and 1 <= i - broke_at <= 5 and broke_level > 0:
                if l[i] <= broke_level * 1.02 and c[i] >= broke_level:
                    buy[i] = True
            if ll10[i - 1] is not None and c[i] < ll10[i - 1]:
                sell[i] = True

    elif key == "atr_breakout":
        a, hh20, ma10 = atr(h, l, c, 14), rolling_max(c, 20), sma(c, 10)
        for i in range(1, n):
            if a[i - 1] is None or hh20[i - 1] is None:
                continue
            if c[i] > c[i - 1] + 0.8 * a[i - 1] and c[i] > hh20[i - 1]:
                buy[i] = True
            if ma10[i] and c[i] < ma10[i]:
                sell[i] = True

    elif key == "nr7_breakout":
        n7 = nr7(h, l, 7)
        for i in range(1, n):
            if n7[i - 1] and c[i] > h[i - 1]:
                buy[i] = True
            if c[i] < l[i - 1]:
                sell[i] = True

    elif key == "wedge_breakout":
        hh5, ll10 = rolling_max(c, 5), rolling_min(c, 10)
        for i in range(20, n):
            hi_now, hi_prev = max(h[i - 9:i + 1]), max(h[i - 19:i - 9])
            lo_now, lo_prev = min(l[i - 9:i + 1]), min(l[i - 19:i - 9])
            if hi_now < hi_prev and lo_now > lo_prev and hh5[i - 1] is not None and c[i] > hh5[i - 1]:
                buy[i] = True
            if ll10[i - 1] is not None and c[i] < ll10[i - 1]:
                sell[i] = True

    # ================= 量价（11 条） =================
    elif key == "volume_breakout":
        ma10, hh10, v5 = sma(c, 10), rolling_max(c, 10), sma(v, 5)
        for i in range(1, n):
            if c[i - 1] <= 0 or v5[i - 1] in (None, 0) or hh10[i - 1] is None:
                continue
            chg = c[i] / c[i - 1] - 1
            if chg > 0.03 and v[i] > 1.5 * (v5[i - 1] or 0) and c[i] > hh10[i - 1]:
                buy[i] = True
            if ma10[i] and c[i] < ma10[i]:
                sell[i] = True

    elif key == "vol_surge_3d":
        ma10 = sma(c, 10)
        for i in range(3, n):
            if (c[i] > o[i] and c[i - 1] > o[i - 1] and c[i - 2] > o[i - 2]
                    and v[i] > v[i - 1] > v[i - 2]):
                buy[i] = True
            if ma10[i] and c[i] < ma10[i]:
                sell[i] = True

    elif key == "vol_ratio_break":
        ma10, v20, hh10 = sma(c, 10), sma(v, 20), rolling_max(c, 10)
        for i in range(1, n):
            if hh10[i - 1] is None or v20[i - 1] in (None, 0):
                continue
            if v[i] > 2 * (v20[i - 1] or 0) and c[i] > hh10[i - 1]:
                buy[i] = True
            if ma10[i] and c[i] < ma10[i]:
                sell[i] = True

    elif key == "vol_ma_cross":
        v5, v20, ma20 = sma(v, 5), sma(v, 20), sma(c, 20)
        for i in range(1, n):
            if cross(v5, v20, i, True) and ma20[i] and c[i] > ma20[i]:
                buy[i] = True
            if cross(v5, v20, i, False):
                sell[i] = True

    elif key == "vol_ma_expand":
        v5, v20, hh20 = sma(v, 5), sma(v, 20), rolling_max(c, 20)
        for i in range(5, n):
            hold5 = all(v5[j] is not None and v20[j] is not None and v5[j] > v20[j]
                        for j in range(i - 4, i + 1))
            if hold5 and hh20[i - 1] is not None and c[i] > hh20[i - 1]:
                buy[i] = True
            if v5[i] is not None and v20[i] is not None and v5[i] < v20[i]:
                sell[i] = True

    elif key == "obv_new_high":
        ob, ma20 = obv(c, v), sma(c, 20)
        ohh20, oll10 = rolling_max(ob, 20), rolling_min(ob, 10)
        for i in range(1, n):
            if ohh20[i - 1] is not None and ob[i] > ohh20[i - 1] and ma20[i] and c[i] > ma20[i]:
                buy[i] = True
            if oll10[i - 1] is not None and ob[i] < oll10[i - 1]:
                sell[i] = True

    elif key == "obv_ma_cross":
        ob = obv(c, v)
        obma = sma(ob, 20)
        for i in range(1, n):
            if cross(ob, obma, i, True):
                buy[i] = True
            if cross(ob, obma, i, False):
                sell[i] = True

    elif key == "shrink_pullback":
        ma10, ma20, v5 = sma(c, 10), sma(c, 20), sma(v, 5)
        for i in range(1, n):
            if (ma20[i] and c[i] > ma20[i] and v5[i - 1] and v[i] < 0.7 * v5[i - 1]
                    and ma10[i] and l[i] <= ma10[i] * 1.01):
                buy[i] = True
            if ma20[i] and c[i] < ma20[i]:
                sell[i] = True

    elif key == "shrink_bottom_reversal":
        ma20 = sma(c, 20)
        held = 0
        for i in range(20, n):
            vmin20 = min(v[i - 20:i])
            if v[i - 1] == vmin20 and c[i] > o[i] and v[i] > 1.5 * v[i - 1]:
                buy[i] = True
                held = 0
            else:
                held += 1
            if held >= 10 or (ma20[i] and c[i] > ma20[i] and c[i - 1] <= (ma20[i - 1] or 0)):
                if held > 0:
                    sell[i] = True

    elif key == "low_vol_breakout":
        v10, v20, ma10, hh20 = sma(v, 10), sma(v, 20), sma(c, 10), rolling_max(c, 20)
        for i in range(1, n):
            if (v10[i - 1] and v20[i - 1] and v10[i - 1] < v20[i - 1] and v[i] > 2 * v20[i - 1]
                    and hh20[i - 1] is not None and c[i] > hh20[i - 1]):
                buy[i] = True
            if ma10[i] and c[i] < ma10[i]:
                sell[i] = True

    elif key == "vol_price_divergence":
        hh20, ma20 = rolling_max(c, 20), sma(c, 20)
        last_high_i = -1
        for i in range(1, n):
            if hh20[i - 1] is not None and c[i] > hh20[i - 1]:
                if last_high_i >= 0 and v[last_high_i] > 0 and v[i] < 0.8 * v[last_high_i]:
                    sell[i] = True          # 量价背离 → 离场信号
                else:
                    buy[i] = True
                last_high_i = i
            if ma20[i] and c[i] < ma20[i]:
                sell[i] = True

    # ================= 摆动指标（13 条） =================
    elif key in ("macd_gold_low", "macd_gold_any"):
        dif, dea = macd(c)
        for i in range(1, n):
            if cross(dif, dea, i, True) and (key == "macd_gold_any" or dif[i] < 0):
                buy[i] = True
            if cross(dif, dea, i, False):
                sell[i] = True

    elif key == "macd_hist_turn":
        dif, dea = macd(c)
        hist = [dif[i] - dea[i] for i in range(n)]
        for i in range(1, n):
            if hist[i - 1] <= 0 < hist[i]:
                buy[i] = True
            if hist[i - 1] >= 0 > hist[i]:
                sell[i] = True

    elif key == "macd_zero_axis_gold":
        dif, dea = macd(c)
        for i in range(1, n):
            if cross(dif, dea, i, True) and dif[i] > 0:
                buy[i] = True
            if cross(dif, dea, i, False):
                sell[i] = True

    elif key == "macd_double_gold":
        dif, dea = macd(c)
        golds = []
        for i in range(1, n):
            if cross(dif, dea, i, True):
                golds.append((i, dif[i] < 0))
                recent = [g for g in golds if i - g[0] <= 60]
                if len(recent) >= 2 and recent[0][1]:
                    buy[i] = True
            if cross(dif, dea, i, False):
                sell[i] = True

    elif key == "macd_divergence_bull":
        dif, dea = macd(c)
        ll30 = rolling_min(c, 30)
        last_low_i, held = -1, 0
        for i in range(1, n):
            if ll30[i] is not None and c[i] <= ll30[i] and (last_low_i < 0 or c[i] < c[last_low_i]):
                if last_low_i >= 0 and dif[i] > dif[last_low_i]:
                    buy[i] = True
                    held = 0
                last_low_i = i
            else:
                held += 1
            if cross(dif, dea, i, False) or held >= 15:
                if held > 0:
                    sell[i] = True

    elif key == "kdj_low_gold":
        ks, ds = kdj(h, l, c)
        for i in range(1, n):
            if cross(ks, ds, i, True) and ks[i] is not None and ks[i] < 30:
                buy[i] = True
            if cross(ks, ds, i, False) or (ks[i] is not None and ks[i] > 80):
                sell[i] = True

    elif key == "kdj_strong_gold":
        ks, ds = kdj(h, l, c)
        for i in range(1, n):
            if cross(ks, ds, i, True) and ks[i] is not None and ks[i] > 50:
                buy[i] = True
            if cross(ks, ds, i, False):
                sell[i] = True

    elif key == "kdj_j_negative":
        ks, ds = kdj(h, l, c)
        j: list[float | None] = [None] * n
        for i in range(n):
            if ks[i] is not None and ds[i] is not None:
                j[i] = 3 * ks[i] - 2 * ds[i]
        for i in range(1, n):
            if j[i] is None or j[i - 1] is None:
                continue
            if j[i - 1] < 0 <= j[i]:
                buy[i] = True
            if j[i] > 100:
                sell[i] = True

    elif key == "rsi_cross_mid":
        r6, r12 = rsi(c, 6), rsi(c, 12)
        for i in range(1, n):
            if cross(r6, r12, i, True):
                buy[i] = True
            if cross(r6, r12, i, False):
                sell[i] = True

    elif key == "cci_zero_cross":
        cc = cci(h, l, c, 14)
        for i in range(1, n):
            if cc[i] is None or cc[i - 1] is None:
                continue
            if cc[i - 1] <= 0 < cc[i]:
                buy[i] = True
            if cc[i - 1] >= 0 > cc[i]:
                sell[i] = True

    elif key == "williams_cross":
        wr = williams_r(h, l, c, 14)
        for i in range(1, n):
            if wr[i] is None or wr[i - 1] is None:
                continue
            if wr[i - 1] <= -50 < wr[i]:
                buy[i] = True
            if wr[i - 1] >= -50 > wr[i]:
                sell[i] = True

    elif key == "dmi_gold":
        pdi, mdi, adx = dmi_adx(h, l, c, 14)
        for i in range(1, n):
            if pdi[i] is None or pdi[i - 1] is None or mdi[i] is None or mdi[i - 1] is None:
                continue
            if (pdi[i - 1] <= mdi[i - 1] and pdi[i] > mdi[i]
                    and adx[i] is not None and adx[i] > 20):
                buy[i] = True
            if pdi[i - 1] >= mdi[i - 1] and pdi[i] < mdi[i]:
                sell[i] = True

    # ================= 形态 · K 线组合 / 缺口 / 连板（11 条） =================
    elif key == "gap_up_break":
        for i in range(1, n):
            if l[i] > h[i - 1] and c[i] > o[i]:
                buy[i] = True
            if c[i] < h[i - 1]:
                sell[i] = True

    elif key == "gap_down_revert":
        gap_top, gap_i = None, -1
        for i in range(1, n):
            if h[i] < l[i - 1]:
                gap_top, gap_i = l[i - 1], i
            if gap_top is not None and i > gap_i and c[i] > gap_top:
                buy[i] = True
            if gap_i >= 1 and gap_top is not None and c[i] >= h[gap_i - 1]:
                sell[i] = True
            if gap_top is not None and i - gap_i >= 10:
                sell[i] = True

    elif key == "gap_hold_unfilled":
        gap_bottom, gap_i = None, -1
        for i in range(1, n):
            if l[i] > h[i - 1]:
                gap_bottom, gap_i = h[i - 1], i
            if gap_i >= 0 and i >= gap_i + 3 and all(l[j] > gap_bottom for j in range(gap_i, i + 1)):
                buy[i] = True
            if gap_bottom is not None and c[i] < gap_bottom:
                sell[i] = True

    elif key == "hammer_bottom":
        ll20 = rolling_min(c, 20)
        hammer_low = None
        for i in range(1, n):
            body = abs(c[i] - o[i])
            lower = min(o[i], c[i]) - l[i]
            small = body <= (h[i] - l[i]) * 0.35
            if ll20[i] is not None and l[i] <= ll20[i] and lower >= 2 * body and small:
                buy[i] = True
                hammer_low = l[i]
            if hammer_low is not None and c[i] < hammer_low:
                sell[i] = True

    elif key == "bullish_engulf":
        ll20 = rolling_min(c, 20)
        engulf_low = None
        for i in range(1, n):
            if (c[i] > o[i] and c[i - 1] < o[i - 1] and c[i] > o[i - 1] and o[i] < c[i - 1]
                    and ll20[i] is not None and min(c[i], o[i]) <= ll20[i] * 1.03):
                buy[i] = True
                engulf_low = l[i - 1]
            if engulf_low is not None and c[i] < engulf_low:
                sell[i] = True

    elif key == "morning_star":
        star_low = None
        for i in range(2, n):
            b0 = c[i - 2] - o[i - 2]              # 第 1 日阴线 → <0
            b1 = abs(c[i - 1] - o[i - 1])         # 第 2 日小实体
            b2 = c[i] - o[i]                      # 第 3 日阳线 → >0
            r1 = (h[i - 1] - l[i - 1]) or 1e-9
            first_mid = (o[i - 2] + c[i - 2]) / 2
            if b0 < 0 and b1 <= r1 * 0.5 and b2 > 0 and c[i] > first_mid:
                buy[i] = True
                star_low = l[i - 1]
            if star_low is not None and c[i] < star_low:
                sell[i] = True

    elif key == "three_white_soldiers":
        t3_low = None
        for i in range(2, n):
            up3 = c[i] > o[i] and c[i - 1] > o[i - 1] and c[i - 2] > o[i - 2]
            rising = c[i] > c[i - 1] > c[i - 2]
            opens_in = (o[i - 2] < o[i - 1] < c[i - 2]) and (o[i - 1] < o[i] < c[i - 1])
            if up3 and rising and opens_in:
                buy[i] = True
                t3_low = min(o[i], c[i])
            if t3_low is not None and c[i] < t3_low:
                sell[i] = True

    elif key == "consecutive_limit_up":
        thr = limit_pct(code, name) * 0.99
        for i in range(2, n):
            if c[i - 1] > 0 and c[i - 2] > 0:
                chg1 = c[i] / c[i - 1] - 1
                chg0 = c[i - 1] / c[i - 2] - 1
                if chg1 >= thr and chg0 >= thr:
                    buy[i] = True
            if c[i - 1] > 0 and c[i] < c[i - 1] * (1 + thr):
                sell[i] = True

    elif key == "first_limit_up":
        thr = limit_pct(code, name) * 0.99
        held = 0
        for i in range(1, n):
            chg = (c[i] / c[i - 1] - 1) if c[i - 1] > 0 else 0.0
            prev_lim = any((c[j] / c[j - 1] - 1) >= thr
                           for j in range(max(1, i - 20), i) if c[j - 1] > 0)
            if chg >= thr and not prev_lim:
                buy[i] = True
                held = 0
            else:
                held += 1
            if held >= 3 or c[i] < c[i - 1]:
                if held > 0:
                    sell[i] = True

    elif key == "w_bottom":
        # 近似识别：40 日窗口分两半各取最低点作"双底"，两低点接近 + 中间有反弹，
        # 再等收盘突破两低点之间的最高价（颈线）确认。
        ma20 = sma(c, 20)
        for i in range(40, n):
            seg = c[i - 40:i + 1]
            l1 = min(range(0, 20), key=lambda j: seg[j])
            l2 = min(range(20, len(seg)), key=lambda j: seg[j])
            lo_a, lo_b = min(l1, l2), max(l1, l2)
            if seg[lo_a] > 0 and lo_b - lo_a >= 10 and abs(seg[lo_b] - seg[lo_a]) / seg[lo_a] < 0.05:
                neck = max(seg[lo_a:lo_b + 1])
                if c[i] > neck and c[i - 1] <= neck:
                    buy[i] = True
        for i in range(1, n):
            if ma20[i] and c[i] < ma20[i] * 0.99:
                sell[i] = True

    elif key == "head_shoulder_bottom":
        ma20 = sma(c, 20)
        for i in range(40, n):
            seg = c[i - 40:i + 1]
            head = min(range(len(seg)), key=lambda j: seg[j])
            if head < 6 or head > len(seg) - 7:
                continue
            left = min(range(0, head - 3), key=lambda j: seg[j])
            right = min(range(head + 4, len(seg)), key=lambda j: seg[j])
            if seg[head] < seg[left] and seg[head] < seg[right]:
                neck = max(max(seg[left:head]) if head > left else seg[left],
                           max(seg[head:right]) if right > head else seg[right])
                if c[i] > neck and c[i - 1] <= neck:
                    buy[i] = True
        for i in range(1, n):
            if ma20[i] and c[i] < ma20[i] * 0.99:
                sell[i] = True

    # ================= 波动率与风险控制（9 条） =================
    elif key == "atr_trailing_stop":
        ma20, a = sma(c, 20), atr(h, l, c, 14)
        highest = None
        for i in range(1, n):
            if ma20[i] is None:
                continue
            if ma20[i] and c[i] > ma20[i] and ma20[i - 1] is not None and c[i - 1] <= ma20[i - 1]:
                buy[i] = True
                highest = c[i]
            if highest is not None:
                highest = max(highest, c[i])
                if a[i] is not None and c[i] < highest - 2 * a[i]:
                    sell[i] = True
                    highest = None

    elif key == "fixed_stop_loss":
        ma20 = sma(c, 20)
        entry = None
        for i in range(1, n):
            if ma20[i] and c[i] > ma20[i] and ma20[i - 1] is not None and c[i - 1] <= ma20[i - 1]:
                buy[i] = True
                entry = c[i]
            if entry is not None and c[i] <= entry * 0.92:
                sell[i] = True
                entry = None

    elif key == "trailing_drawdown_stop":
        ma20 = sma(c, 20)
        peak = None
        for i in range(1, n):
            if ma20[i] and c[i] > ma20[i] and ma20[i - 1] is not None and c[i - 1] <= ma20[i - 1]:
                buy[i] = True
                peak = c[i]
            if peak is not None:
                peak = max(peak, c[i])
                if c[i] <= peak * 0.90:
                    sell[i] = True
                    peak = None

    elif key == "time_stop_20d":
        ma20 = sma(c, 20)
        entry_i, entry = -1, None
        for i in range(1, n):
            if ma20[i] and c[i] > ma20[i] and ma20[i - 1] is not None and c[i - 1] <= ma20[i - 1]:
                buy[i] = True
                entry_i, entry = i, c[i]
            if entry is not None and i - entry_i >= 20 and c[i] <= entry:
                sell[i] = True
                entry = None

    elif key == "boll_squeeze":
        bw = boll_width(c, 20, 2.0)
        for i in range(60, n):
            win = [bw[j] for j in range(i - 59, i + 1) if bw[j] is not None]
            if bw[i] is None or not win:
                continue
            if bw[i] <= min(win) + 1e-9:
                buy[i] = True
            if bw[i] >= sorted(win)[len(win) // 2]:
                sell[i] = True

    elif key == "boll_squeeze_breakout":
        mid, up, dn = boll(c, 20, 2.0)
        bw = boll_width(c, 20, 2.0)
        for i in range(60, n):
            win = [bw[j] for j in range(i - 59, i + 1) if bw[j] is not None]
            if up[i] is None or not win:
                continue
            recent = [bw[j] for j in range(max(0, i - 4), i + 1) if bw[j] is not None]
            if recent and min(recent) <= min(win) + 1e-9 and c[i] > up[i]:
                buy[i] = True
            if mid[i] and c[i] < mid[i]:
                sell[i] = True

    elif key == "hist_vol_filter":
        vol = _realized_vol(c, 20)
        for i in range(1, n):
            if vol[i] is None:
                continue
            if vol[i] > 25:
                buy[i] = True
            if vol[i] < 15:
                sell[i] = True

    # ================= 动量与相对强弱（6 条） =================
    elif key in ("momentum_20", "momentum_60"):
        p, thr = (20, 0.10) if key == "momentum_20" else (60, 0.20)
        for i in range(p, n):
            if c[i - p] <= 0:
                continue
            mom = c[i] / c[i - p] - 1
            if mom > thr:
                buy[i] = True
            if mom < 0:
                sell[i] = True

    elif key == "momentum_risk_adj":
        ret = [0.0] * n
        for i in range(1, n):
            ret[i] = (c[i] / c[i - 1] - 1) if c[i - 1] > 0 else 0.0
        for i in range(20, n):
            if c[i - 20] <= 0:
                continue
            mom = c[i] / c[i - 20] - 1
            w = ret[i - 19:i + 1]
            m = sum(w) / len(w)
            sd = (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5
            ratio = (mom / sd) if sd > 0 else 0.0
            if ratio > 1.5:
                buy[i] = True
            if ratio < 0:
                sell[i] = True

    elif key == "momentum_accel":
        for i in range(20, n):
            if c[i - 10] <= 0 or c[i - 20] <= 0:
                continue
            r10 = c[i] / c[i - 10] - 1
            r20 = c[i] / c[i - 20] - 1
            if r10 > r20 and r20 > 0:
                buy[i] = True
            if r10 < 0:
                sell[i] = True

    elif key == "relative_strength_20":
        ic = (ctx or {}).get("idx_c")
        if not ic:
            return buy, sell
        ic = _align(ic, n)
        for i in range(20, n):
            if c[i - 20] <= 0 or not ic[i - 20]:
                continue
            exc = (c[i] / c[i - 20] - 1) - (ic[i] / ic[i - 20] - 1)
            if exc > 0.05:
                buy[i] = True
            if exc < 0:
                sell[i] = True

    # ================= 组合 / 仓位 / 择时（5 条） =================
    elif key == "index_ma20_timing":
        ic = (ctx or {}).get("idx_c")
        if not ic:
            return buy, sell
        ic = _align(ic, n)
        ima = sma(ic, 20)
        for i in range(1, n):
            if ima[i] is None:
                continue
            if ic[i] > ima[i]:
                buy[i] = True
            else:
                sell[i] = True

    elif key == "index_ma250_timing":
        ic = (ctx or {}).get("idx_c")
        if not ic:
            return buy, sell
        ic = _align(ic, n)
        ima = sma(ic, 250)
        for i in range(1, n):
            if ima[i] is None:
                continue
            if ic[i] > ima[i]:
                buy[i] = True
            else:
                sell[i] = True

    elif key == "breadth_timing":
        br = (ctx or {}).get("breadth")
        if not br:
            return buy, sell
        br = _align(br, n, fill=1.0)
        for i in range(1, n):
            if br[i] is None:
                continue
            if br[i] > 0.50:
                buy[i] = True
            if br[i] < 0.30:
                sell[i] = True

    elif key == "vol_regime_timing":
        ic = (ctx or {}).get("idx_c")
        if not ic:
            return buy, sell
        ic = _align(ic, n)
        vol = _realized_vol(ic, 20)
        for i in range(1, n):
            if vol[i] is None:
                continue
            if vol[i] < 25:
                buy[i] = True
            if vol[i] > 35:
                sell[i] = True

    return buy, sell


# ---------------------------------------------------------------- 回测
def backtest(buy, sell, c) -> dict:
    """单标的：满仓进出，按收盘价成交（**旧口径，保留作内部备用**）。

    对外一律走 backtest_window（次日成交 + 可成交性过滤）。
    """
    cash, pos = 1.0, 0.0
    entry = 0.0
    trades = wins = 0
    peak = 1.0
    mdd = 0.0
    eq_end = 1.0
    for i in range(len(c)):
        if pos == 0 and buy[i]:
            pos, cash, entry = cash / c[i], 0.0, c[i]
        elif pos > 0 and sell[i]:
            cash, pos = pos * c[i], 0.0
            trades += 1
            if c[i] > entry:
                wins += 1
        eq = cash + pos * c[i]
        eq_end = eq
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd
    return {"ret": eq_end - 1.0, "mdd": mdd, "trades": trades, "wins": wins, "cancelled": 0}


def _is_ex_rights(c: list, j: int, code: str, name: str, dates: list) -> bool:
    """疑似除权除息日（close-to-close 跳空远超板块涨跌停且非涨跌停）—— 价格被复权方式扭曲。

    无复权口径下，送转/除息会让次日收盘价凭空跳空（如 10 送 10 → 约 -35%），
    若按此日成交会产生虚假买卖点。该跳空幅度远超任何合法涨跌停，故可据此识别并跳过。
    """
    if j <= 0 or j >= len(c):
        return False
    pc, cc = c[j - 1], c[j]
    if not pc or pc <= 0 or not cc:
        return False
    g = cc / pc - 1.0
    try:
        from board_rules import limit_pct as _br_limit_pct
        d = dates[j] if j < len(dates) else None
        lim = _br_limit_pct(code, name, d)
    except Exception:  # noqa: BLE001
        lim = 0.10
    if abs(g) <= lim * 1.5:
        return False
    # 排除正常涨跌停（涨跌停本身就是合法价格，不是扭曲）
    return not (abs(g) >= lim * 0.985)


def backtest_window(buy, sell, o, h, l, c, code: str = "", name: str = "",
                    lo: int = 0, hi: int | None = None,
                    dates: list | None = None) -> dict:
    """挂单模型：**判定在 i、成交在 i+1、按 c[i+1] 成交**。

    - 信号在 i 日收盘确定 → 挂单给下一根 bar；
    - 次日若为一字板（`executable` 返回 False）→ 撤单跳过本笔，计入 cancelled；
    - 最后一根 bar 的挂单自然作废（无 i+1）；
    - 回测区间限定在 [lo, hi]（窗口切片），预热段仅供指标计算。
    """
    nsr = len(c)
    if hi is None:
        hi = nsr - 1
    cash, pos, entry = 1.0, 0.0, 0.0
    trades = wins = cancelled = ex_skipped = 0
    peak = 1.0
    mdd = 0.0
    eq_end = 1.0
    pending = 0                       # +1 待买 / -1 待卖
    lo = max(0, min(lo, nsr - 1))
    hi = max(lo, min(hi, nsr - 1))
    for i in range(lo, hi + 1):
        prev_c = c[i - 1] if i > 0 else c[i]
        # ① 先执行上一根 bar 挂下的单（按今日收盘成交）
        if pending == 1 and pos == 0:
            if executable(o[i], h[i], l[i], c[i], prev_c, code, name):
                pos, cash, entry = cash / c[i], 0.0, c[i]
            else:
                cancelled += 1        # 一字涨停买不到 → 撤单，本笔放弃
            pending = 0
        elif pending == -1 and pos > 0:
            cash, pos = pos * c[i], 0.0
            trades += 1
            if c[i] > entry:
                wins += 1
            pending = 0
        # ② 再按今日收盘判定信号 → 挂给下一根 bar
        if pos == 0 and buy[i] and pending == 0:
            # 修复 P0-01：若「执行日 i+1」疑似除权除息（close 跳空远超板块涨跌停且非涨跌停），
            # 该日价格被复权方式扭曲，按此信号成交会产生虚假买卖点 → 强制跳过并计数上报。
            if i + 1 < nsr and dates is not None and _is_ex_rights(c, i + 1, code, name, dates):
                ex_skipped += 1
            else:
                pending = 1
        elif pos > 0 and sell[i]:
            pending = -1
        # ③ 记净值/回撤
        eq = cash + pos * c[i]
        eq_end = eq
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd
    # 2026-09-26 真实性修正：期末是否仍持仓（窗口最后一根 bar 时 pos>0）。
    # 用于区分「一次都没交易」与「买了但到窗口末还没卖」——后者 trades 计数为 0，但不是"没动过"。
    open_at_end = bool(pos > 0)
    return {"ret": eq_end - 1.0, "mdd": mdd, "trades": trades, "wins": wins,
            "cancelled": cancelled, "ex_skipped": ex_skipped, "open_at_end": open_at_end}


def bench_return(k: list[dict]) -> float | None:
    if len(k) < 2 or k[0]["c"] <= 0:
        return None
    return k[-1]["c"] / k[0]["c"] - 1.0


# ---------------------------------------------------------------- 建表
def ensure_tables(con: sqlite3.Connection) -> None:
    """按设计文档 §12 附录建两张新表（幂等）。旧 strategy_sim 表不动。"""
    con.executescript("""
        CREATE TABLE IF NOT EXISTS strategy_sim_window (
          strategy_key  TEXT NOT NULL,
          window_label  TEXT NOT NULL,
          bars          INTEGER,
          start         TEXT,
          end           TEXT,
          total_return  REAL,
          annual_return REAL,
          max_dd        REAL,
          win_rate      REAL,
          trades        INTEGER,
          beat_bench    REAL,
          bench_return  REAL,
          stocks        INTEGER,
          score         REAL,
          rank          INTEGER,
          rating        TEXT,
          status        TEXT,
          sample_note   TEXT,
          best_json     TEXT,
          worst_json    TEXT,
          computed_at   TEXT,
          PRIMARY KEY (strategy_key, window_label)
        );
        CREATE TABLE IF NOT EXISTS strategy_sim_meta (
          id          INTEGER PRIMARY KEY CHECK (id = 1),
          generated_at TEXT,
          universe    INTEGER,
          bars_fetch  INTEGER,
          bench_code  TEXT,
          bench_return REAL,
          start       TEXT,
          end         TEXT,
          caliber_json TEXT,
          note        TEXT
        );
    """)
    # 2026-09-26 兼容「已存在且有数据」的旧表：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
    # 故显式查 PRAGMA 后按需 ALTER 追加 open_at_end 列（幂等，重复运行安全）。
    cols = {row[1] for row in con.execute("PRAGMA table_info(strategy_sim_window)").fetchall()}
    if "open_at_end" not in cols:
        con.execute("ALTER TABLE strategy_sim_window ADD COLUMN open_at_end INTEGER")
    # 2026-10-15 P0-2 / DEF-007：walk-forward 样本外字段（幂等追加，兼容旧表）。
    for col, ctype in (
        ("walk_forward", "TEXT"),
        ("in_sample_return", "REAL"),
        ("oos_return", "REAL"),
        ("oos_beat_bench", "REAL"),
        ("oos_trades", "INTEGER"),
        ("oos_status", "TEXT"),
        ("oos_validated", "INTEGER"),
    ):
        if col not in cols:
            con.execute(f"ALTER TABLE strategy_sim_window ADD COLUMN {col} {ctype}")


# ---------------------------------------------------------------- 主流程
def _liquid_from_kline(limit: int) -> list:
    """从本地日线全库（data/kline_daily.db）算近 60 交易日平均成交额，流动性降序返回。

    2026-10-30 新增。背景（BUG-20261030）：旧 universe() 的主路径依赖
    stock_liquidity 表，而该表在本机**从未建过**；异常被 except 吞掉后走
    「positions ∪ lhb_stocks」兜底。positions 清理后为 0 行，lhb_stocks 只剩
    703 只「历史上过龙虎榜」的票 —— 于是预测候选池永久卡在 594~734 只
    （prediction_run.pool_n 铁证：13 天恒为 157，其后 594/658/687/734），
    POOL_LIMIT 从 160 调到 2000 完全没生效。现改为读本地 1700 万行日线，
    覆盖全市场 5500+ 只，且不依赖网络。
    """
    if not os.path.exists(KLINE_DB):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % KLINE_DB, uri=True)
        try:
            last = con.execute("SELECT MAX(d) FROM kline").fetchone()[0]
            if not last:
                return []
            start = con.execute(
                "SELECT MIN(d) FROM (SELECT DISTINCT d FROM kline "
                "WHERE d<=? ORDER BY d DESC LIMIT 60)", (last,)).fetchone()[0]
            rows = con.execute(
                "SELECT code, AVG(c*v) AS amt FROM kline "
                "WHERE d>=? AND d<=? AND c>0 AND v>0 "
                "GROUP BY code ORDER BY amt DESC LIMIT ?",
                (start, last, int(limit))).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 —— 日线库缺失/损坏，交给上层继续降级
        return []
    out = []
    for c, _amt in rows:
        s = str(c or "").strip()
        # 与 selection._bad_code 同口径：剔北交所（4/8/9 开头，含 920xxx）
        if len(s) == 6 and s.isdigit() and s[0] in "036":
            out.append(s)
    return out


def universe(limit: int):
    """标的池（2026-10-30 重写：全市场流动性排序，不再卡在几百只）。

    来源优先级（前一级失败才降级，降级会打日志，绝不静默）：
      ① stock_liquidity 表（近 60 自然日成交额，若存在）
      ② 本地日线全库算近 60 交易日平均成交额  ← 当前实际主路径
      ③ 干净宇宙全量（all_ashares − ST/无K线）
    返回 (codes, names, boards)；codes 保持流动性降序，剔除 ST 后再截 limit。

    2026-10-30 D35 **删掉了原来的第 ④ 级「lhb_stocks 龙虎榜历史兜底」**：
    `viewer/avoid.py` 的 4077 交易日 / 37.3 万样本已经证明**「上榜」这个事件本身没有信息量**
    （近10日上榜 T+5 超额 +0.006%、t=0.07，等于零；有分辨力的是换手率）。拿它凑候选池
    = 用噪声选股，而且它的覆盖只有历史上过榜的股票（比全市场窄一个数量级），
    一旦被触发就是"页面正常、结论全错"的静默降级 —— 比空池更危险。
    现在 ③ 之后若仍为空，**直接返回空池并告警**，由调用方报错，不再用别的东西凑数。
    """
    names = {}
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            for c, n in con.execute("SELECT code, name FROM stock_names"):
                names[str(c)] = n
        except Exception:  # noqa: BLE001
            pass
        con.close()
    except Exception:  # noqa: BLE001
        pass

    try:
        from viewer.universe_filter import get_exclusion_pool, all_ashares
    except Exception:  # noqa: BLE001
        get_exclusion_pool = all_ashares = None
    # 2026-10-31 整改（测评 P0-B）：原 `except Exception: excluded = set()` 是 fail-open。
    # 回测里"不剔除 ST"会直接污染收益分布，而且这个污染**无法从结果里看出来**
    # —— 它表现为"策略更赚钱"，最容易被当成好消息。
    excluded: set = set()
    if get_exclusion_pool is not None:
        from viewer.universe_filter import get_exclusion_pool_strict, ExclusionPoolUnavailable
        try:
            excluded, _, _ = get_exclusion_pool_strict()
        except ExclusionPoolUnavailable as _e:
            # 回测可以拒绝跑，但不可以悄悄跑一份"含 ST"的结果。
            raise SystemExit("[strategy_sim][FATAL] 剔除池不可用，拒绝产出含 ST 的回测：%s" % _e)

    def _keep(cands):
        return [c for c in cands if len(c) == 6 and c.isdigit() and c[0] in "036"]

    codes: list = []
    src = "none"

    # ① 流动性采样表
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT code FROM stock_liquidity "
                "WHERE trade_date >= date('now','-60 day') "
                "GROUP BY code ORDER BY AVG(amount) DESC LIMIT ?",
                (limit,)).fetchall()
        finally:
            con.close()
        cand = _keep([str(x[0] or "").strip() for x in rows])
        if cand:
            codes, src = cand, "stock_liquidity"
    except Exception:  # noqa: BLE001
        pass

    # ② 本地日线全库流动性（取足候选再剔 ST，避免剔除后不足 limit）
    if not codes:
        cand = _liquid_from_kline(max(int(limit), 6000))
        if cand:
            codes, src = cand, "kline_liquidity"

    # ③ 干净宇宙全量
    if not codes and all_ashares is not None:
        try:
            cand = _keep(sorted(set(all_ashares())))
            if cand:
                codes, src = cand, "clean_universe"
        except Exception:  # noqa: BLE001
            pass

    # ④ 已删除（2026-10-30 D35）：原「lhb_stocks 龙虎榜历史兜底」。
    #    理由见 docstring：上榜事件本身 t=0.07 无信息量，拿它凑池等于用噪声选股，
    #    且覆盖远窄于全市场。宁可空池报错，也不做这种静默降级。
    if not codes:
        print("[universe] 错误：①流动性表 ②本地日线 ③干净宇宙 全部取不到候选，"
              "返回空池（已拒绝用龙虎榜历史凑数，见 D35）", flush=True)
        return [], names, {}

    if excluded:
        codes = [c for c in codes if c not in excluded]
    ordered = codes[:limit] if limit and limit > 0 else codes
    if len(ordered) < 200:
        print("[universe] 警告：候选池仅 %d 只（来源=%s），覆盖严重不足"
              % (len(ordered), src), flush=True)
    boards = {c: board_of(c) for c in ordered}
    return ordered, names, boards


def _annualize(total_frac: float, bars: int) -> float:
    years = bars / 244.0 if bars else 1.0
    if years > 0 and total_frac > -1:
        return (1 + total_frac) ** (1 / years) - 1
    return total_frac


def _binom_surv_p(n: int, k: int) -> float:
    """二项存活 p 值（单侧：样本外跑赢基准比例 > 0.5 的显著性），正态近似，零外部依赖。
    P1-21 多重检验：每只策略样本外「跑赢基准笔数」在 H0(胜率=50%) 下的 p 值。"""
    if n < 1:
        return 1.0
    p_hat = k / n
    se = math.sqrt(0.25 / n)
    if se <= 0:
        return 1.0
    z = (p_hat - 0.5) / se
    # 单侧存活：P(Z >= z) = 0.5 * erfc(z / sqrt(2))
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _bh_fdr(pvals: list, alpha: float = 0.05) -> list:
    """Benjamini-Hochberg FDR 校正，返回与 pvals 等长的布尔列表（True=通过校正/显著）。
    P1-21：对全部策略的样本外 p 值做多重检验，控制假发现率，避免「测 100 个策略总有几个碰巧显著」。"""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    keep = [False] * m
    prev_keep = False
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        thr = alpha * rank / m
        if pvals[i] <= thr or prev_keep:
            keep[i] = True
            prev_keep = True
    return keep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=160)
    ap.add_argument("--bars", type=int, default=FETCH_BARS)
    ap.add_argument("--targets", type=str, default="",
                    help="仅回测这些 key（逗号分隔），用于分批判查")
    args = ap.parse_args()

    t0 = time.time()
    codes, names, boards = universe(args.limit)
    print(f"[1/5] 标的池 {len(codes)} 只，每只目标 {args.bars} 根日线", flush=True)

    # --- 抓取（关键：缓存根数不足则重抓，否则三窗口会被同一段旧缓存切出来）---
    cache = load_kcache()
    klines: dict[str, list[dict]] = {}
    fetched = hit = 0
    for i, code in enumerate(codes, 1):
        ck = cache.get(code)
        rows = ck.get("rows") if ck else None
        # 2026-10-15 P0-2 / DEF-006：缓存必须带正确 adj 标记才复用，避免不复权旧批次污染回测。
        if rows and len(rows) >= args.bars and ck.get("adj") == ADJ_CACHE_OK:
            klines[code] = rows[-args.bars:]
            hit += 1
            continue
        try:
            new_rows = fetch_kline(code, args.bars)
        except Exception:  # noqa: BLE001
            new_rows = []
        if len(new_rows) >= 60:
            klines[code] = new_rows
            cache[code] = {"rows": new_rows, "at": now_cst_str(),
                           "adj": ADJ_CACHE_OK}
            fetched += 1
        elif rows and len(rows) >= 60:
            klines[code] = rows              # 抓取失败但旧缓存可用 → 退回旧缓存（仍按原 adj 复用）
            hit += 1
        time.sleep(GAP)
        if i % 40 == 0:
            print(f"      ..{i}/{len(codes)}（新抓 {fetched} 只 / 命中缓存 {hit} 只）", flush=True)
    save_kcache(cache)
    print(f"      有效日线 {len(klines)} 只（新抓 {fetched} 只 / 命中缓存 {hit} 只）", flush=True)
    if not klines:
        print("  无可用日线，退出", flush=True)
        return 1

    # --- 基准（上证指数）---
    idx = cache.get("000001")
    irows = (idx or {}).get("rows")
    if not irows or len(irows) < args.bars or (idx or {}).get("adj") != ADJ_CACHE_OK:
        try:
            irows = fetch_kline("000001", args.bars)
            cache["000001"] = {"rows": irows, "at": now_cst_str(),
                                "adj": ADJ_CACHE_OK}
            save_kcache(cache)
        except Exception:  # noqa: BLE001
            irows = irows or []
    else:
        irows = irows[-args.bars:]
    idx_c = [r["c"] for r in irows]
    bench_all = bench_return(irows) or 0.0
    print(f"[2/5] 基准（上证指数 000001）整段 {bench_all*100:+.2f}%（{len(irows)} 根）", flush=True)

    # 市场宽度：每个指数日，池内收盘站上 MA20 的比例（供 breadth_timing 用）
    stock_series = {}
    for code, k in klines.items():
        cc = [r["c"] for r in k]
        ma = sma(cc, 20)
        stock_series[code] = {k[j]["d"]: (cc[j], ma[j]) for j in range(len(k))}
    breadth: list[float | None] = [None] * len(irows)
    for bi, row in enumerate(irows):
        num = den = 0
        for code, mp in stock_series.items():
            vv = mp.get(row["d"])
            if vv and vv[1] is not None:
                den += 1
                if vv[0] > vv[1]:
                    num += 1
        breadth[bi] = (num / den) if den else None
    ctx = {"idx_c": idx_c, "breadth": breadth}

    sys.path.insert(0, BASE)
    from strategy_data import STRATEGIES  # noqa: PLC0415

    targets = {x.strip() for x in args.targets.split(",") if x.strip()} if args.targets else None
    active = [st for st in STRATEGIES
              if st.get("backtestable") and not st.get("scope_limited")
              and (targets is None or st["key"] in targets)]
    print(f"[3/5] 回测 {len(active)} 条策略 × {len(klines)} 只标的 × {len(WINDOWS)} 窗口", flush=True)

    # 基准各窗口收益（同一次抓取切片，天然可比）
    bench_by_window = {}
    for wl, wb in WINDOWS.items():
        sub = irows[-wb:] if len(irows) >= wb else irows
        bench_by_window[wl] = (sub[-1]["c"] / sub[0]["c"] - 1.0) if len(sub) >= 2 and sub[0]["c"] > 0 else 0.0

    now = now_cst_str()
    rows_out = []
    oos_pvals = []  # P1-21：收集样本外二项 p 值，循环后做 BH-FDR 多重检验
    for st in active:
        key = st["key"]
        sig = {}
        for code, k in klines.items():
            try:
                sig[code] = build_signals(key, k, ctx, code, names.get(code, ""))
            except Exception:  # noqa: BLE001
                sig[code] = ([False] * len(k), [False] * len(k))
        per_win = {}
        for wl, wb in WINDOWS.items():
            rets, mdds = [], []
            in_rets = []                      # 2026-10-15 P0-2 / DEF-007：样本内（前 70%）
            oos_rets = []                     # 2026-10-15 P0-2 / DEF-007：样本外（后 30%）
            trades = wins = cancelled = open_n = 0
            oos_trades = 0
            best = worst = None
            start_d, end_d = "", ""
            for code, k in klines.items():
                buy, sell = sig[code]
                lo = max(0, len(k) - wb)
                hi = len(k) - 1
                r = backtest_window(buy, sell,
                                    [x["o"] for x in k], [x["h"] for x in k],
                                    [x["l"] for x in k], [x["c"] for x in k],
                                    code, names.get(code, ""), lo, hi)
                rets.append(r["ret"])
                mdds.append(r["mdd"])
                trades += r["trades"]
                wins += r["wins"]
                cancelled += r["cancelled"]
                if r.get("open_at_end"):
                    open_n += 1
                if best is None or r["ret"] > best["ret"]:
                    best = {"code": code, "name": names.get(code, code), "ret": round(r["ret"], 4)}
                if worst is None or r["ret"] < worst["ret"]:
                    worst = {"code": code, "name": names.get(code, code), "ret": round(r["ret"], 4)}
                # —— DEF-007 walk-forward：窗口按 70/30 切（前段训 / 后段验），同参数同信号 ——
                split = lo + max(1, int((hi - lo) * WF_IN_SAMPLE_FRAC))
                ri = backtest_window(buy, sell,
                                     [x["o"] for x in k], [x["h"] for x in k],
                                     [x["l"] for x in k], [x["c"] for x in k],
                                     code, names.get(code, ""), lo, split,
                                     dates=[x["d"] for x in k])
                ro = backtest_window(buy, sell,
                                     [x["o"] for x in k], [x["h"] for x in k],
                                     [x["l"] for x in k], [x["c"] for x in k],
                                     code, names.get(code, ""), split, hi,
                                     dates=[x["d"] for x in k])
                in_rets.append(ri["ret"])
                oos_rets.append(ro["ret"])
                oos_trades += ro["trades"]
                if k:
                    wstart, wend = k[lo]["d"], k[hi]["d"]
                    if not start_d or wstart < start_d:
                        start_d = wstart
                    if not end_d or wend > end_d:
                        end_d = wend
            ns = len(rets)
            avg = sum(rets) / ns if ns else 0.0
            avg_mdd = sum(mdds) / ns if ns else 0.0
            in_avg = sum(in_rets) / ns if ns else 0.0
            oos_avg = sum(oos_rets) / ns if ns else 0.0
            # 2026-09-26 真实性修正：0 笔了结交易时胜率是「未定义」，不是 0（0 会被误读成"胜率 0%"）。
            win_rate = (wins / trades * 100.0) if trades else None
            bench_w = bench_by_window[wl]
            beat = (sum(1 for x in rets if x > bench_w) / ns * 100.0) if ns else 0.0
            oos_beat = (sum(1 for x in oos_rets if x > bench_w) / ns * 100.0) if ns else 0.0
            bars_eff = min(wb, max((len(k) for k in klines.values()), default=0))
            # DEF-007 样本外验证判定：样本外需有足够交易笔数；且与样本内同向（不反号）才可信。
            # 样本外若明显转负/微弱，提示「疑似过拟合」——界面据此给灰度角标而非盲目「已验证」。
            if ns == 0 or oos_trades < OOS_MIN_TRADES:
                oos_status = "insufficient"
                oos_validated = False
            else:
                oos_status = "ok"
                # P1-21：删除原「abs(oos_avg)<0.03 即放行」的宽限分支——它会在样本外微弱转负时
                # 仍判「已验证」，与应用过拟合信号（疑似过拟合角标）自相矛盾。现严格判定：
                # 样本外必须为正 且 与样本内同向，才认作「已验证」。
                oos_validated = (oos_avg > 0) and ((in_avg > 0) == (oos_avg > 0))
                # P1-21：采集样本外二项 p 值，供循环后 BH-FDR 多重检验校正
                if wl == "mid" and oos_trades >= OOS_MIN_TRADES:
                    _k = round(oos_beat / 100.0 * oos_trades)
                    oos_pvals.append((key, _binom_surv_p(oos_trades, _k)))
            per_win[wl] = {
                "bars": bars_eff, "start": start_d, "end": end_d,
                "total_return": avg * 100.0, "max_dd": avg_mdd * 100.0,
                "annual_return": _annualize(avg, bars_eff) * 100.0,
                "win_rate": win_rate, "trades": trades, "wins": wins, "cancelled": cancelled,
                "ex_skipped": ex_skipped,
                "open_at_end": bool(open_n > 0),
                "beat_bench": beat, "bench_return": bench_w * 100.0, "stocks": ns,
                "best": best, "worst": worst,
                "status": ("no_data" if ns == 0 else ("insufficient" if trades < MIN_TRADES else "ok")),
                # 2026-10-15 P0-2 / DEF-007：walk-forward 70/30 样本外字段
                "walk_forward": "70/30",
                "in_sample_return": round(in_avg * 100.0, 2),
                "oos_return": round(oos_avg * 100.0, 2),
                "oos_beat_bench": round(oos_beat, 1),
                "oos_trades": oos_trades,
                "oos_status": oos_status,
                "oos_validated": oos_validated,
            }
        m = per_win["mid"]
        m_wr = m["win_rate"] if m["win_rate"] is not None else 0.0
        m["score"] = m["annual_return"] - abs(m["max_dd"]) * 0.6 + (m_wr - 50) * 0.2
        rows_out.append({"key": key, "name": st["name"], "school": st["school"], "per_win": per_win})
        print(f"      {st['name'][:22]:<24} mid 收益{m['total_return']:+7.2f}% 回撤{m['max_dd']:7.2f}% "
              f"胜率{(m['win_rate'] or 0):5.1f}% 交易{m['trades']:>4} → score {m['score']:6.2f}", flush=True)

    # P1-21：BH-FDR 多重检验校正——对全部策略样本外 p 值统一控制假发现率。
    # 未通过校正的策略即便单看样本外为正，也下调为「疑似过拟合」，避免多重比较碰巧显著。
    if oos_pvals:
        _pv = [p for _, p in oos_pvals]
        _keep = _bh_fdr(_pv, alpha=0.05)
        _fdr_fail = {kk for (kk, _), ok in zip(oos_pvals, _keep) if not ok}
        for r in rows_out:
            if r["key"] in _fdr_fail:
                _mw = r["per_win"].get("mid")
                if _mw and _mw.get("oos_validated"):
                    _mw["oos_validated"] = False
                    _mw["oos_status"] = "fdr_rejected"

    # 仅 mid 窗口排名
    ranked = sorted(rows_out, key=lambda x: -x["per_win"]["mid"]["score"])
    order = ["优秀", "良好", "一般", "较差"]
    N = len(ranked)
    for i, r in enumerate(ranked):
        mid = r["per_win"]["mid"]
        mid["rank"] = i + 1
        mid["rating"] = order[min(3, i * 4 // max(1, N))]

    print("[4/5] 落库 strategy_sim_window / strategy_sim_meta（旧 strategy_sim 不写）", flush=True)
    con = sqlite3.connect(DB, timeout=30)
    ensure_tables(con)
    con.execute("DELETE FROM strategy_sim_window")
    con.execute("DELETE FROM strategy_sim_meta")
    for r in rows_out:
        for wl in ("mid", "y1", "h1"):
            w = r["per_win"][wl]
            note_parts = [f"交易 {w['trades']} 笔"]
            # 2026-09-26：0 笔了结且期末仍持仓 → 说明「不是没交易，是还没卖」（不覆盖既有内容，仅追加）。
            if w["trades"] == 0 and w.get("open_at_end"):
                note_parts.append("期末仍持仓·未了结")
            if w["cancelled"]:
                note_parts.append(f"一字板撤单 {w['cancelled']} 笔")
            if w["trades"] < MIN_TRADES:
                note_parts.append("样本不足")
            # 2026-10-15 P0-2 / DEF-007：样本外验证状态追加到采样说明。
            if w.get("walk_forward"):
                if w.get("oos_status") == "insufficient":
                    note_parts.append("样本外交易不足·未验证")
                elif w.get("oos_validated"):
                    note_parts.append("已样本外验证(70/30)")
                elif w.get("oos_status") == "fdr_rejected":
                    note_parts.append("样本外多重检验(BH-FDR)未通过·疑似过拟合")
                else:
                    note_parts.append("样本外转弱·疑似过拟合")
            sample_note = "；".join(note_parts)
            con.execute(
                """INSERT INTO strategy_sim_window
                   (strategy_key,window_label,bars,start,end,total_return,annual_return,max_dd,
                    win_rate,open_at_end,trades,beat_bench,bench_return,stocks,score,rank,rating,status,
                    sample_note,best_json,worst_json,computed_at,
                    walk_forward,in_sample_return,oos_return,oos_beat_bench,oos_trades,oos_status,oos_validated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["key"], wl, w["bars"], w["start"], w["end"],
                 round(w["total_return"], 2), round(w["annual_return"], 2), round(w["max_dd"], 2),
                 (round(w["win_rate"], 1) if w["win_rate"] is not None else None),
                 (1 if w.get("open_at_end") else 0),
                 w["trades"], round(w["beat_bench"], 1),
                 round(w["bench_return"], 2), w["stocks"],
                 (round(w["score"], 2) if wl == "mid" else None),
                 (w.get("rank") if wl == "mid" else None),
                 (w.get("rating") if wl == "mid" else None),
                 w["status"], sample_note,
                 json.dumps(w["best"], ensure_ascii=False),
                 json.dumps(w["worst"], ensure_ascii=False), now,
                 w.get("walk_forward"), w.get("in_sample_return"), w.get("oos_return"),
                 w.get("oos_beat_bench"), w.get("oos_trades"), w.get("oos_status"),
                 (1 if w.get("oos_validated") else 0)))

    caliber = {
        "title": "回测口径",
        "text": ("等权单标的满仓、信号次日收盘成交、不计手续费与滑点；"
                 "用于策略相对强弱比较，不是可实现收益预测。"),
        "fill": "next_close",
        "limit_up_filter": True,
        # 2026-10-22 P0-01 整改：新浪接口忽略 adj 参数，实测返回「新浪日线不复权」，如实标注
        "adj": "新浪日线不复权",
        "bench": {"code": "000001", "name": "上证指数"},
        "bars_fetch": args.bars,
        # 2026-10-15 P0-2 / DEF-007：walk-forward 样本外验证（界面据此给角标）
        "walk_forward": {
            "split": "70% 训练 / 30% 验证（按时间序切分）",
            "note": ("每个窗口按时间序切 70% 训练 / 30% 验证，样本外只验一次；"
                     "样本外收益单独展示，样本外转负/微弱标记「疑似过拟合」。实盘通常较样本内低 30%-50%，勿直接外推。"),
        },
        # 主参考窗口自述（王总 2026-10-06 定的口径）：调参只吃这一段，更早的不算数
        "main_window": {
            "label": MAIN_WINDOW_LABEL,
            "years": LOOKBACK_YEARS,
            "bars": WINDOWS[MAIN_WINDOW_LABEL],
            "note": ("主参考窗口＝近 %.1f 年（约 %d 个交易日）；评分 / 排名 / 评级只看这一窗口，"
                     "更早的行情不参与统计。y1 / h1 仅作辅助对照，不参与调参。"
                     % (LOOKBACK_YEARS, WINDOWS[MAIN_WINDOW_LABEL])),
        },
    }
    meta_start = min((k[0]["d"] for k in klines.values() if k), default="")
    meta_end = max((k[-1]["d"] for k in klines.values() if k), default="")
    con.execute(
        """INSERT INTO strategy_sim_meta
           (id,generated_at,universe,bars_fetch,bench_code,bench_return,start,end,caliber_json,note)
           VALUES (1,?,?,?,?,?,?,?,?,?)""",
        (now, len(klines), args.bars, "000001", round(bench_all * 100, 2), meta_start, meta_end,
         json.dumps(caliber, ensure_ascii=False),
         "等权单标的满仓、信号次日收盘成交、不计手续费与滑点；仅作策略相对强弱比较。"
         "数据源＝新浪日线后复权(hfq)；每个窗口按 70/30 做样本外验证（实盘通常较样本内低 30%-50%，勿直接外推）。"))
    con.commit()
    con.close()

    payload = {"generated_at": now, "universe": len(klines), "bars_fetch": args.bars,
               "bench_return": round(bench_all * 100, 2), "caliber": caliber,
               "rows": [{"key": r["key"], "name": r["name"], "school": r["school"],
                         "mid": {k: v for k, v in r["per_win"]["mid"].items() if k not in ("best", "worst")}}
                        for r in ranked]}
    json.dump(payload, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[5/5] 完成：{len(active)} 策略 × 3 窗口 = {len(active)*3} 行，用时 {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
