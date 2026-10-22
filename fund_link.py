#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""策略 × 公募基金 关联引擎（净路口径）—— fund_link.py

用途
----
为「策略回测」板块的每条策略，回答一个问题：**这条策略对应哪些基金？**

口径（必须如实对外标注，不可省）
--------------------------------
1. **候选池**：东方财富基金排行「股票型 / 混合型 / 指数型」按**近一年收益降序**各取前 N 只
   （`--pool-per-type`，默认 200）。这是**有偏的池**（偏近期强势），不是"全市场基金"，
   界面上必须写明池口径，不可表述为"全市场筛选结果"。
2. **价格序列**：用**累计净值（LJJZ）**日序列，不用单位净值（DWJZ）。
   理由：DWJZ 在分红除权日会人为跳空下跌，会被误判成"破位/超卖"，产生假信号；
   累计净值在分红日不发生跳变，序列连续，适合做技术指标。
   → **代价**：累计净值不是"分红再投"的复权净值，长期收益率会被系统性低估（少算分红再投部分）。
   本项目只把它用于"信号判定"，不用于宣称收益。
3. **信号可判定性**：原策略清单里只有一部分**仅凭一条收盘价格序列**即可判定。
   - `exact`：原规则本身就只用收盘价（均线/EMA/RSI/MACD/新高/动量/标准差…）→ 净口径等价。
   - `approx`：原规则需要**日内最高/最低价**（CCI / 威廉 %R / KDJ / 布林突破回踩 / NR7 等）
     → 用收盘价的滚动极值替代，**属近似判定，界面必须标「近似」**。
   - `none`：需要**成交量、日内 OHLC、资金流向、龙虎榜、基本面、板块或事件**数据
     → **基金净值序列无法判定**，如实返回不可判定 + 缺什么，绝不编造。
4. **「当前命中」定义**：最近 `RECENT_DAYS`（默认 5）个交易日内出现过该策略的**买入信号**。
   记录信号日、信号日至今净值涨跌，供肉眼复核。

数据来源（逐条可追溯）
----------------------
- 基金清单：`fund.eastmoney.com/data/rankhandler.aspx`（东方财富基金排行）
- 基金净值：`fund.eastmoney.com/pingzhongdata/<code>.js`（东方财富基金详情，内嵌累计净值 ACWorthTrend 时序，无需 cookie）
- 策略规则：本仓库 `strategy_data.py`（策略目录唯一真源）

落库
----
- `fund_strategy_signal`：策略 × 基金 命中明细（引擎写，接口只读）
- `fund_strategy_meta`：单行口径与生成时间

依赖：**纯 Python 标准库**（与项目既有铁律一致，不引第三方）。
用法：
    python fund_link.py                      # 全量（默认 600 只候选基金）
    python fund_link.py --pool-per-type 100  # 缩小候选池（快速验证）
    python fund_link.py --refresh            # 忽略当日净值缓存，强制重抓
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sqlite3
import ssl
import sys
import time
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, 'viewer', 'app.db')
CACHE_PATH = os.path.join(ROOT, 'fund_nav_cache.json')
CACHE_OHLC_PATH = os.path.join(ROOT, 'fund_ohlcv_cache.json')
RESULT_PATH = os.path.join(ROOT, 'fund_link_result.json')

RECENT_DAYS = 5          # 最近 N 个交易日内触发信号 → 视为"当前命中"
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
SRC_RANK = '东方财富基金排行 fund.eastmoney.com/data/rankhandler.aspx'
SRC_NAV = '东方财富基金详情 fund.eastmoney.com/pingzhongdata/<code>.js（累计净值 ACWorthTrend）'
SRC_INNER = '新浪财经场内涵数清单 vip.stock.finance.sina.com.cn（ETF/LOF，按成交额降序）'
SRC_KLINE = '新浪财经场内涵数日线 money.finance.sina.com.cn（OHLC + 成交量）'

import netctx  # noqa: E402  —— 统一出网 TLS 口径（整改 #27）
_CTX = netctx.https_context("fund.eastmoney.com")  # 整改 #27：默认不校验，按环境变量可开启
# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http_get(url: str, referer: str, timeout: int = 30, retry: int = 2) -> str:
    last = None
    for _ in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': UA,
                'Referer': referer,
                'Accept': '*/*',
                'Accept-Encoding': 'gzip, deflate',
            })
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                raw = r.read()
                if (r.headers.get('Content-Encoding') or '').lower() == 'gzip':
                    raw = gzip.decompress(raw)
                return raw.decode('utf-8', errors='ignore')
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise RuntimeError('HTTP 失败 %s：%s' % (url[:90], last))


# ---------------------------------------------------------------------------
# 1) 候选基金池
# ---------------------------------------------------------------------------

FUND_TYPES = (('gp', '股票型'), ('hh', '混合型'), ('zs', '指数型'))


def fetch_pool(per_type: int, throttle: float) -> list[dict]:
    pool: dict[str, dict] = {}
    for ft, tname in FUND_TYPES:
        got, page = 0, 1
        while got < per_type:
            pn = min(100, per_type - got)
            url = ('https://fund.eastmoney.com/data/rankhandler.aspx?op=ph&dt=kf&ft=%s'
                   '&rs=&gs=0&sc=1nzf&st=desc&pi=%d&pn=%d&dx=1&v=0.1' % (ft, page, pn))
            txt = http_get(url, 'https://fund.eastmoney.com/data/fundranking.html')
            m = re.search(r'datas:\[(.*?)\],allRecords', txt, re.S)
            rows = re.findall(r'"([^"]*)"', m.group(1)) if m else []
            if not rows:
                break
            for row in rows:
                f = row.split(',')
                if len(f) < 4:
                    continue
                code, name = f[0].strip(), f[1].strip()
                if not code or code in pool:
                    continue
                pool[code] = {'code': code, 'name': name, 'type': tname,
                              'market': '场外', 'rank_in_type': got + 1}
                got += 1
            page += 1
            time.sleep(throttle)
            if page > 12:
                break
        print('  [池] %-4s 取到 %d 只' % (tname, got), flush=True)
    return list(pool.values())


# ---------------------------------------------------------------------------
# 2) 基金净值（累计净值优先）
# ---------------------------------------------------------------------------


def fetch_nav(code: str, bars: int, throttle: float) -> list[tuple[str, float]]:
    """返回按日期升序的 [(date, 累计净值)]。

    来源：东方财富基金详情 `fund.eastmoney.com/pingzhongdata/<code>.js`
    内嵌的 `Data_ACWorthTrend`（累计净值时序，[毫秒时间戳, 累计净值]）。
    该接口无需 cookie，比 f10/lsjz 稳定。
    """
    url = 'https://fund.eastmoney.com/pingzhongdata/%s.js' % code
    txt = http_get(url, 'https://fundf10.eastmoney.com/jjjz_%s.html' % code)
    time.sleep(throttle)
    m = re.search(r'Data_ACWorthTrend\s*=\s*(\[.*?\]);', txt, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[str, float]] = []
    for row in arr:
        try:
            ts, v = row[0], row[1]
            d = datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d')
            out.append((d, float(v)))
        except Exception:  # noqa: BLE001
            continue
    if bars and len(out) > bars:
        out = out[-bars:]     # 取末 bars 根（升序）
    return out


# ---------------------------------------------------------------------------
# 2b) 场内涵数（ETF/LOF）—— 唯一带成交量的公募载体
# ---------------------------------------------------------------------------

SINA_NODES = (('etf_hq_fund', '场内ETF'), ('lof_hq_fund', '场内LOF'))


def fetch_inner_pool(per_node: int, throttle: float) -> list[dict]:
    """场内涵数清单（ETF/LOF），按成交额降序各取前 N。

    用途：量能类策略（OBV / 放量突破 …）与日内形态策略需要 OHLC + 成交量，
    场外基金净值序列没有这些字段，只有场内 ETF/LOF 才有 → 单列一个池。
    """
    pool: dict[str, dict] = {}
    for node, tname in SINA_NODES:
        got, page = 0, 1
        while got < per_node:
            num = min(100, per_node - got)
            url = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
                   'Market_Center.getHQNodeData?page=%d&num=%d&sort=amount&asc=0&node=%s'
                   % (page, num, node))
            try:
                txt = http_get(url, 'https://vip.stock.finance.sina.com.cn/mkt/',
                               timeout=20, retry=1)
                rows = json.loads(txt)
            except Exception:  # noqa: BLE001
                break
            if not rows:
                break
            for r in rows:
                code = str(r.get('code') or '').strip()
                sym = str(r.get('symbol') or '').strip().lower()
                if len(code) != 6 or sym[:2] not in ('sh', 'sz'):
                    continue
                if code in pool:
                    continue
                pool[code] = {'code': code, 'name': str(r.get('name') or '').strip(),
                              'type': tname, 'market': '场内', 'sym': sym}
                got += 1
            page += 1
            time.sleep(throttle)
            if page > 12:
                break
        print('  [池] %-8s 取到 %d 只（按成交额降序）' % (tname, got), flush=True)
    return list(pool.values())


def fetch_ohlcv(sym: str, bars: int, throttle: float) -> list[tuple]:
    """场内日线 [(date, open, high, low, close, volume)] 升序。

    来源：新浪财经 `CN_MarketData.getKLineData`（scale=240 即日线）。
    """
    url = ('https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           'CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d' % (sym, bars))
    try:
        txt = http_get(url, 'https://finance.sina.com.cn/', timeout=20, retry=1)
    except Exception:  # noqa: BLE001
        return []
    time.sleep(throttle)
    try:
        rows = json.loads(txt)
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple] = []
    for r in rows or []:
        try:
            out.append((str(r['day'])[:10], float(r['open']), float(r['high']),
                        float(r['low']), float(r['close']), float(r['volume'])))
        except Exception:  # noqa: BLE001
            continue
    if bars and len(out) > bars:
        out = out[-bars:]
    return out


# ---------------------------------------------------------------------------
# 3) 指标（单序列，纯收盘/累计净值口径）
# ---------------------------------------------------------------------------


def sma(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    s = 0.0
    for i, v in enumerate(xs):
        s += v
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    k = 2.0 / (n + 1)
    prev = None
    for i, v in enumerate(xs):
        prev = v if prev is None else (v * k + prev * (1 - k))
        out[i] = prev
    return out


def rsi(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    if n >= len(xs):
        return out
    gains = losses = 0.0
    for i in range(1, len(xs)):
        ch = xs[i] - xs[i - 1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            gains += g
            losses += l
            if i == n:
                ag, al = gains / n, losses / n
                out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
            out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(xs: list[float], fast=12, slow=26, sig=9):
    ef, es = ema(xs, fast), ema(xs, slow)
    dif = [(ef[i] - es[i]) if (ef[i] is not None and es[i] is not None) else None
           for i in range(len(xs))]
    dif_f = [d if d is not None else 0.0 for d in dif]
    dea = ema(dif_f, sig)
    hist = [(dif_f[i] - dea[i]) * 2 if dea[i] is not None else None for i in range(len(xs))]
    return dif, dea, hist


def roll_max(xs: list[float], n: int) -> list[float | None]:
    return [None if i < n - 1 else max(xs[i - n + 1:i + 1]) for i in range(len(xs))]


def roll_min(xs: list[float], n: int) -> list[float | None]:
    return [None if i < n - 1 else min(xs[i - n + 1:i + 1]) for i in range(len(xs))]


def stdev(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = (sum((v - m) ** 2 for v in w) / n) ** 0.5
    return out


def kdj_approx(xs: list[float], n=9, m1=3, m2=3, hi=None, lo=None):
    """KDJ。

    传入 hi/lo（真实日内高低）时与原规则等价；否则用收盘价的滚动极值近似。
    """
    hh = roll_max(hi if hi is not None else xs, n)
    ll = roll_min(lo if lo is not None else xs, n)
    kk: list[float | None] = [None] * len(xs)
    dd: list[float | None] = [None] * len(xs)
    jj: list[float | None] = [None] * len(xs)
    k = d = 50.0
    for i in range(len(xs)):
        if hh[i] is None or ll[i] is None or hh[i] == ll[i]:
            continue
        rsv = (xs[i] - ll[i]) / (hh[i] - ll[i]) * 100
        k = (m1 - 1) / m1 * k + 1 / m1 * rsv
        d = (m2 - 1) / m2 * d + 1 / m2 * k
        kk[i], dd[i], jj[i] = k, d, 3 * k - 2 * d
    return kk, dd, jj


def cci_approx(xs: list[float], n=14, hi=None, lo=None) -> list[float | None]:
    """CCI。

    传入 hi/lo 时典型价 TP=(H+L+C)/3（与原规则等价）；否则 TP 用收盘价近似。
    """
    if hi is not None and lo is not None:
        tp = [(hi[i] + lo[i] + xs[i]) / 3 for i in range(len(xs))]
    else:
        tp = xs
    out: list[float | None] = [None] * len(xs)
    ma = sma(tp, n)
    for i in range(n - 1, len(xs)):
        if ma[i] is None:
            continue
        w = tp[i - n + 1:i + 1]
        md = sum(abs(v - ma[i]) for v in w) / n
        out[i] = 0.0 if md == 0 else (tp[i] - ma[i]) / (0.015 * md)
    return out


def williams_approx(xs: list[float], n=14, hi=None, lo=None) -> list[float | None]:
    """威廉 %R（-100~0）。传入 hi/lo 时用真实高低；否则用收盘极值近似。"""
    hh = roll_max(hi if hi is not None else xs, n)
    ll = roll_min(lo if lo is not None else xs, n)
    out: list[float | None] = [None] * len(xs)
    for i in range(len(xs)):
        if hh[i] is None or ll[i] is None or hh[i] == ll[i]:
            continue
        out[i] = (hh[i] - xs[i]) / (hh[i] - ll[i]) * -100
    return out


def true_range(xs: list[float], hi=None, lo=None) -> list[float]:
    """真实 TR。

    有日内高低 → max(H−L, |H−C_prev|, |L−C_prev|)（与原规则等价）；
    无 → 仅用 |ΔC| 近似。
    """
    n = len(xs)
    if n == 0:
        return []
    if hi is None or lo is None:
        return [0.0] + [abs(xs[i] - xs[i - 1]) for i in range(1, n)]
    out = [hi[0] - lo[0]]
    for i in range(1, n):
        out.append(max(hi[i] - lo[i], abs(hi[i] - xs[i - 1]), abs(lo[i] - xs[i - 1])))
    return out


def atr_approx(xs: list[float], n=14, hi=None, lo=None) -> list[float | None]:
    return sma(true_range(xs, hi, lo), n)


def obv(cs: list[float], vs: list[float]) -> list[float]:
    """能量潮 OBV：按收盘涨跌方向累加成交量。"""
    out: list[float] = []
    acc = 0.0
    for i, c in enumerate(cs):
        if i == 0:
            out.append(0.0)
            continue
        if c > cs[i - 1]:
            acc += vs[i]
        elif c < cs[i - 1]:
            acc -= vs[i]
        out.append(acc)
    return out


def dmi(hi: list[float], lo: list[float], cs: list[float], n: int = 14):
    """DMI：返回 (+DI, −DI, ADX)。Wilder 平滑，需真实日内高低。"""
    m = len(cs)
    pdm = [0.0] * m
    mdm = [0.0] * m
    tr = [0.0] * m
    for i in range(1, m):
        up = hi[i] - hi[i - 1]
        dn = lo[i - 1] - lo[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(hi[i] - lo[i], abs(hi[i] - cs[i - 1]), abs(lo[i] - cs[i - 1]))
    pdi: list[float | None] = [None] * m
    mdi: list[float | None] = [None] * m
    adx: list[float | None] = [None] * m
    if m <= n:
        return pdi, mdi, adx
    sp = sum(pdm[1:n + 1])
    sm = sum(mdm[1:n + 1])
    st = sum(tr[1:n + 1])
    dxs: list[float] = []
    for i in range(n, m):
        if i > n:
            sp = sp - sp / n + pdm[i]
            sm = sm - sm / n + mdm[i]
            st = st - st / n + tr[i]
        if st <= 0:
            continue
        p = 100 * sp / st
        mm = 100 * sm / st
        pdi[i], mdi[i] = p, mm
        s = p + mm
        dxs.append(100 * abs(p - mm) / s if s > 0 else 0.0)
        if len(dxs) == n:
            adx[i] = sum(dxs) / n
        elif len(dxs) > n:
            adx[i] = (adx[i - 1] * (n - 1) + dxs[-1]) / n if adx[i - 1] is not None else None
    return pdi, mdi, adx


# ---------------------------------------------------------------------------
# 4) 信号判定
# ---------------------------------------------------------------------------

BUY_SIGNALS: dict[str, tuple[str, str]] = {}     # key -> (caliber, 人话口径)
SIGNAL_FNS: dict[str, object] = {}               # key -> 判定函数


def reg(key: str, caliber: str, note: str):
    def deco(fn):
        BUY_SIGNALS[key] = (caliber, note)
        SIGNAL_FNS[key] = fn
        return fn
    return deco


def _cross(a, b, i) -> bool:
    if i < 1:
        return False
    x0, x1 = a[i - 1], a[i]
    y0, y1 = b[i - 1], b[i]
    if x0 is None or x1 is None or y0 is None or y1 is None:
        return False
    return x0 <= y0 and x1 > y1


@reg('ma_cross_20_60', 'exact', 'MA20 上穿 MA60（仅需收盘序列，与原规则等价）')
def _s_ma_cross_20_60(x, i, C):
    return _cross(C['ma20'], C['ma60'], i)


@reg('ma_cross_5_20', 'exact', 'MA5 上穿 MA20')
def _s_ma_cross_5_20(x, i, C):
    return _cross(C['ma5'], C['ma20'], i)


@reg('ma_cross_10_30', 'exact', 'MA10 上穿 MA30')
def _s_ma_cross_10_30(x, i, C):
    return _cross(C['ma10'], C['ma30'], i)


@reg('ma_cross_10_60', 'exact', 'MA10 上穿 MA60')
def _s_ma_cross_10_60(x, i, C):
    return _cross(C['ma10'], C['ma60'], i)


@reg('ma_cross_second_gold', 'exact', 'MA20 上穿 MA60，且此前 60 日内出现过死叉')
def _s_ma_cross_second_gold(x, i, C):
    if not _cross(C['ma20'], C['ma60'], i):
        return False
    for k in range(max(1, i - 60), i):
        if _cross(C['ma60'], C['ma20'], k):
            return True
    return False


@reg('ema_cross_12_26', 'exact', 'EMA12 上穿 EMA26')
def _s_ema_cross_12_26(x, i, C):
    return _cross(C['ema12'], C['ema26'], i)


@reg('ema_cross_20_50', 'exact', 'EMA20 上穿 EMA50')
def _s_ema_cross_20_50(x, i, C):
    return _cross(C['ema20'], C['ema50'], i)


@reg('ma_bull_align', 'exact', 'MA5>MA20>MA60 且当日收盘重新站上 MA5')
def _s_ma_bull_align(x, i, C):
    a, b, c = C['ma5'][i], C['ma20'][i], C['ma60'][i]
    if None in (a, b, c) or not (a > b > c):
        return False
    p = C['ma5'][i - 1] if i >= 1 else None
    return x[i] > a and p is not None and x[i - 1] <= p


@reg('ma_bull_align_strict', 'exact', 'MA5>MA10>MA20>MA60 且收盘>MA5')
def _s_ma_bull_align_strict(x, i, C):
    v = [C['ma5'][i], C['ma10'][i], C['ma20'][i], C['ma60'][i]]
    if any(u is None for u in v) or not (v[0] > v[1] > v[2] > v[3]):
        return False
    return x[i] > v[0]


@reg('pullback_ma20', 'approx', 'MA20 向上、收盘落在 MA20 上方 ±2% 内（原规则用最低价，净口径以收盘价近似）')
def _s_pullback_ma20(x, i, C):
    m = C['ma20'][i]
    m3 = C['ma20'][i - 3] if i >= 3 else None
    if m is None or m3 is None or not m > m3:
        return False
    return x[i] > m and (x[i] - m) / m <= 0.02


@reg('pullback_ma10', 'approx', 'MA10 向上、收盘落在 MA10 上方 ±2% 内（收盘价近似）')
def _s_pullback_ma10(x, i, C):
    m = C['ma10'][i]
    m3 = C['ma10'][i - 3] if i >= 3 else None
    if m is None or m3 is None or not m > m3:
        return False
    return x[i] > m and (x[i] - m) / m <= 0.02


@reg('pullback_ma60', 'approx', 'MA60 向上、收盘落在 MA60 上方 ±3% 内（收盘价近似）')
def _s_pullback_ma60(x, i, C):
    m = C['ma60'][i]
    m3 = C['ma60'][i - 3] if i >= 3 else None
    if m is None or m3 is None or not m > m3:
        return False
    return x[i] > m and (x[i] - m) / m <= 0.03


@reg('price_above_ma250', 'exact', '收盘 > MA250 且 MA250 较 20 日前不下移')
def _s_price_above_ma250(x, i, C):
    m, m20 = C['ma250'][i], (C['ma250'][i - 20] if i >= 20 else None)
    if m is None or m20 is None:
        return False
    return x[i] > m and m >= m20


@reg('rsi_reversal', 'exact', 'RSI6 曾 < 20 后重新回升')
def _s_rsi_reversal(x, i, C):
    r = C['rsi6']
    if i < 1 or None in (r[i], r[i - 1]):
        return False
    return r[i - 1] < 20 and r[i] > r[i - 1]


@reg('rsi2_extreme', 'exact', 'RSI2 < 10')
def _s_rsi2_extreme(x, i, C):
    v = C['rsi2'][i]
    return v is not None and v < 10


@reg('boll_lower', 'exact', '收盘曾跌破下轨、随后收回下轨之上')
def _s_boll_lower(x, i, C):
    lo = C['boll_lo']
    if i < 1 or None in (lo[i], lo[i - 1]):
        return False
    return x[i - 1] < lo[i - 1] and x[i] > lo[i]


@reg('boll_lower_double', 'exact', '连续 2 日收盘 < 下轨')
def _s_boll_lower_double(x, i, C):
    lo = C['boll_lo']
    if i < 1 or None in (lo[i], lo[i - 1]):
        return False
    return x[i] < lo[i] and x[i - 1] < lo[i - 1]


@reg('mean_revert_ma20', 'exact', '收盘低于 MA20 达 12% 以上')
def _s_mean_revert_ma20(x, i, C):
    m = C['ma20'][i]
    return m is not None and (x[i] - m) / m <= -0.12


@reg('mean_revert_ma60', 'exact', '收盘低于 MA60 达 18% 以上')
def _s_mean_revert_ma60(x, i, C):
    m = C['ma60'][i]
    return m is not None and (x[i] - m) / m <= -0.18


@reg('zscore_revert_20', 'exact', 'Z-Score(20) < −2')
def _s_zscore_revert_20(x, i, C):
    m, s = C['ma20'][i], C['sd20'][i]
    if m is None or s is None or s == 0:
        return False
    return (x[i] - m) / s < -2


@reg('cci_oversold', 'approx', 'CCI(14) 上穿 −100（典型价用收盘价近似）')
def _s_cci_oversold(x, i, C):
    c = C['cci']
    if i < 1 or None in (c[i], c[i - 1]):
        return False
    return c[i - 1] <= -100 and c[i] > -100


@reg('williams_oversold', 'approx', '%R(14) 上穿 −80（滚动高低用收盘价近似）')
def _s_williams_oversold(x, i, C):
    w = C['wr']
    if i < 1 or None in (w[i], w[i - 1]):
        return False
    return w[i - 1] <= -80 and w[i] > -80


@reg('bias_revert_6', 'exact', 'BIAS6 < −8%')
def _s_bias_revert_6(x, i, C):
    m = C['ma6'][i]
    return m is not None and (x[i] - m) / m <= -0.08


@reg('consecutive_down_4', 'exact', '连续 4 个交易日收跌')
def _s_consecutive_down_4(x, i, C):
    if i < 4:
        return False
    return all(x[i - k] < x[i - k - 1] for k in range(4))


@reg('break_10d_high', 'exact', '收盘创 10 个交易日新高')
def _s_break_10d_high(x, i, C):
    h = C['hh10'][i - 1] if i >= 1 else None
    return h is not None and x[i] > h


@reg('break_20d_high', 'exact', '收盘创 20 个交易日新高')
def _s_break_20d_high(x, i, C):
    h = C['hh20'][i - 1] if i >= 1 else None
    return h is not None and x[i] > h


@reg('break_60d_high', 'exact', '收盘创 60 个交易日新高')
def _s_break_60d_high(x, i, C):
    h = C['hh60'][i - 1] if i >= 1 else None
    return h is not None and x[i] > h


@reg('break_120d_high', 'exact', '收盘创 120 个交易日新高')
def _s_break_120d_high(x, i, C):
    h = C['hh120'][i - 1] if i >= 1 else None
    return h is not None and x[i] > h


@reg('turtle_donchian_20_10', 'exact', '收盘突破 20 日最高（收盘确认口径）')
def _s_turtle_20(x, i, C):
    return _s_break_20d_high(x, i, C)


@reg('turtle_donchian_55_20', 'exact', '收盘突破 55 日最高')
def _s_turtle_55(x, i, C):
    h = C['hh55'][i - 1] if i >= 1 else None
    return h is not None and x[i] > h


@reg('box_breakout', 'approx', '前 20 日振幅 < 12%（振幅以收盘极差近似）且收盘 > 前 20 日最高')
def _s_box_breakout(x, i, C):
    hi, lo = C['hh20'][i - 1] if i >= 1 else None, C['ll20'][i - 1] if i >= 1 else None
    if hi is None or lo is None or lo == 0:
        return False
    return (hi / lo - 1) < 0.12 and x[i] > hi


@reg('atr_breakout', 'approx', '收盘 > 昨收 + 0.8×ATR14 且创 20 日新高（ATR 用 |ΔC| 近似）')
def _s_atr_breakout(x, i, C):
    a = C['atr14'][i - 1] if i >= 1 else None
    if a is None:
        return False
    return _s_break_20d_high(x, i, C) and x[i] > x[i - 1] + 0.8 * a


@reg('nr7_breakout', 'approx', '前 1 日为近 7 日最小波动（以 |ΔC| 近似）且收盘 > 前 1 日收盘')
def _s_nr7_breakout(x, i, C):
    tr = C['tr']
    if i < 8:
        return False
    if tr[i - 1] > min(tr[i - 7:i]):
        return False
    return x[i] > x[i - 1]


@reg('macd_gold_low', 'exact', 'DIF 上穿 DEA 且 DIF < 0')
def _s_macd_gold_low(x, i, C):
    d, e = C['dif'], C['dea']
    return _cross(d, e, i) and d[i] is not None and d[i] < 0


@reg('macd_gold_any', 'exact', 'DIF 上穿 DEA')
def _s_macd_gold_any(x, i, C):
    return _cross(C['dif'], C['dea'], i)


@reg('macd_hist_turn', 'exact', 'MACD 柱由负转正')
def _s_macd_hist_turn(x, i, C):
    h = C['hist']
    if i < 1 or None in (h[i], h[i - 1]):
        return False
    return h[i - 1] < 0 <= h[i]


@reg('macd_zero_axis_gold', 'exact', 'DIF 上穿 DEA 且 DIF > 0')
def _s_macd_zero_axis_gold(x, i, C):
    d = C['dif']
    return _cross(C['dif'], C['dea'], i) and d[i] is not None and d[i] > 0


@reg('macd_double_gold', 'exact', '60 日内 DIF 二次上穿 DEA，且首次金叉在零轴下')
def _s_macd_double_gold(x, i, C):
    d, e = C['dif'], C['dea']
    if not _cross(d, e, i):
        return False
    for k in range(max(1, i - 60), i):
        if _cross(d, e, k) and d[k] is not None and d[k] < 0:
            return True
    return False


@reg('macd_divergence_bull', 'exact', '收盘创 30 日新低（不含当日）且 DIF 高于上次触底时的 DIF（底背离）')
def _s_macd_divergence_bull(x, i, C):
    if i < 31:
        return False
    ll_prev = C['ll30'][i - 1]          # 前 30 日最低（不含当日）
    d = C['dif']
    if ll_prev is None or d[i] is None:
        return False
    if x[i] > ll_prev:                   # 当日收盘未创 30 日新低 → 非背离起点
        return False
    for k in range(max(1, i - 60), i):
        if x[k] <= (C['ll30'][k] or x[k]) and d[k] is not None and d[i] > d[k]:
            return True                  # 价格新低、DIF 不新低 → 底背离
    return False


@reg('kdj_low_gold', 'approx', 'K 上穿 D 且 K < 30（RSV 高低用收盘价近似）')
def _s_kdj_low_gold(x, i, C):
    k, dd = C['k'], C['d']
    return _cross(k, dd, i) and k[i] is not None and k[i] < 30


@reg('kdj_strong_gold', 'approx', 'K 上穿 D 且 K > 50（近似口径）')
def _s_kdj_strong_gold(x, i, C):
    k, dd = C['k'], C['d']
    return _cross(k, dd, i) and k[i] is not None and k[i] > 50


@reg('kdj_j_negative', 'approx', 'J 上穿 0 且此前 J < 0（近似口径）')
def _s_kdj_j_negative(x, i, C):
    j = C['j']
    if i < 1 or None in (j[i], j[i - 1]):
        return False
    return j[i - 1] < 0 <= j[i]


@reg('rsi_cross_mid', 'exact', 'RSI6 上穿 RSI12')
def _s_rsi_cross_mid(x, i, C):
    return _cross(C['rsi6'], C['rsi12'], i)


@reg('cci_zero_cross', 'approx', 'CCI(14) 上穿 0（典型价用收盘价近似）')
def _s_cci_zero_cross(x, i, C):
    c = C['cci']
    if i < 1 or None in (c[i], c[i - 1]):
        return False
    return c[i - 1] <= 0 < c[i]


@reg('williams_cross', 'approx', '%R(14) 上穿 −50（近似口径）')
def _s_williams_cross(x, i, C):
    w = C['wr']
    if i < 1 or None in (w[i], w[i - 1]):
        return False
    return w[i - 1] <= -50 < w[i]


@reg('boll_squeeze', 'exact', '布林带宽为 60 日最低')
def _s_boll_squeeze(x, i, C):
    w = C['bw']
    if i < 59 or w[i] is None:
        return False
    win = [u for u in w[i - 59:i + 1] if u is not None]
    return bool(win) and w[i] <= min(win)


@reg('boll_squeeze_breakout', 'exact', '前 5 日内带宽为 60 日最低，且当日收盘 > 上轨')
def _s_boll_squeeze_breakout(x, i, C):
    up, w = C['boll_up'], C['bw']
    if up[i] is None or x[i] <= up[i]:
        return False
    for k in range(max(0, i - 4), i + 1):
        if _s_boll_squeeze(x, k, C):
            return True
    return False


@reg('momentum_20', 'exact', '20 日涨幅 > 10%')
def _s_momentum_20(x, i, C):
    return i >= 20 and x[i - 20] > 0 and x[i] / x[i - 20] - 1 > 0.10


@reg('momentum_60', 'exact', '60 日涨幅 > 20%')
def _s_momentum_60(x, i, C):
    return i >= 60 and x[i - 60] > 0 and x[i] / x[i - 60] - 1 > 0.20


@reg('momentum_risk_adj', 'exact', '20 日收益 / 20 日波动率 > 1.5')
def _s_momentum_risk_adj(x, i, C):
    if i < 21 or x[i - 20] <= 0:
        return False
    r = x[i] / x[i - 20] - 1
    rets = [x[k] / x[k - 1] - 1 for k in range(i - 19, i + 1) if x[k - 1] > 0]
    if len(rets) < 10:
        return False
    m = sum(rets) / len(rets)
    sd = (sum((v - m) ** 2 for v in rets) / len(rets)) ** 0.5
    return sd > 0 and r / sd > 1.5


@reg('momentum_accel', 'exact', '10 日收益 > 20 日收益 且 20 日收益 > 0')
def _s_momentum_accel(x, i, C):
    if i < 20 or x[i - 20] <= 0 or x[i - 10] <= 0:
        return False
    r10 = x[i] / x[i - 10] - 1
    r20 = x[i] / x[i - 20] - 1
    return r20 > 0 and r10 > r20


# --- 量能类（需成交量；仅场内涵数可得 → caliber='vol'） ---


@reg('volume_breakout', 'vol', '放量突破：收盘创 20 日新高 且 成交量 ≥ 20 日均量 ×1.5')
def _s_volume_breakout(x, i, C):
    v, vm = C['vol'], C['vma20']
    if i < 20 or vm[i] is None or C['hh20'][i] is None:
        return False
    return x[i] >= C['hh20'][i] and v[i] >= 1.5 * vm[i]


@reg('vol_surge_3d', 'vol', '近 3 日内出现 单日成交量 ≥ 20 日均量 ×2')
def _s_vol_surge_3d(x, i, C):
    v, vm = C['vol'], C['vma20']
    if i < 20:
        return False
    for k in range(max(0, i - 2), i + 1):
        if vm[k] is not None and v[k] >= 2 * vm[k]:
            return True
    return False


@reg('vol_ratio_break', 'vol', '量比 ≥2（当日量 ÷ 5 日均量）且 收盘站上 MA20')
def _s_vol_ratio_break(x, i, C):
    v, vm5, ma20 = C['vol'], C['vma5'], C['ma20']
    if i < 20 or vm5[i] is None or ma20[i] is None or vm5[i] <= 0:
        return False
    return v[i] >= 2 * vm5[i] and x[i] > ma20[i]


@reg('vol_ma_cross', 'vol', '5 日均量 上穿 20 日均量（量能由缩转放）')
def _s_vol_ma_cross(x, i, C):
    return _cross(C['vma5'], C['vma20'], i)


@reg('vol_ma_expand', 'vol', '量能多头排列：5 日均量 > 10 日均量 > 20 日均量')
def _s_vol_ma_expand(x, i, C):
    a, b, c = C['vma5'][i], C['vma10'][i], C['vma20'][i]
    if i < 20 or a is None or b is None or c is None:
        return False
    return a > b > c


@reg('obv_new_high', 'vol', 'OBV 创 60 日新高（能量潮创新高）')
def _s_obv_new_high(x, i, C):
    o, hh = C['obv'], C['obv_hh60']
    if i < 60 or hh[i] is None:
        return False
    return o[i] >= hh[i]


@reg('obv_ma_cross', 'vol', 'OBV 上穿其 30 日均线')
def _s_obv_ma_cross(x, i, C):
    return _cross(C['obv'], C['obv_ma'], i)


@reg('shrink_pullback', 'vol', '缩量回调：上升趋势（收盘 > MA20）+ 当日下跌 + 量 ≤ 20 日均量 ×0.7')
def _s_shrink_pullback(x, i, C):
    v, vm, ma20 = C['vol'], C['vma20'], C['ma20']
    if i < 20 or vm[i] is None or ma20[i] is None:
        return False
    return x[i] > ma20[i] and x[i] < x[i - 1] and v[i] <= 0.7 * vm[i]


@reg('shrink_bottom_reversal', 'vol', '缩量见底：量 ≤ 20 日均量 ×0.6 + 前一日创 10 日新低 + 当日收阳')
def _s_shrink_bottom_reversal(x, i, C):
    v, vm, ll10 = C['vol'], C['vma20'], C['ll10']
    if i < 20 or vm[i] is None or ll10[i - 1] is None:
        return False
    return (v[i] <= 0.6 * vm[i] and x[i] > x[i - 1]
            and x[i - 1] <= ll10[i - 1] * 1.005)


@reg('low_vol_breakout', 'vol', '缩量后突破：前 5 日均量 ≤ 20 日均量 ×0.7 且 收盘创 20 日新高')
def _s_low_vol_breakout(x, i, C):
    v, vm, hh20 = C['vol'], C['vma20'], C['hh20']
    if i < 25 or vm[i] is None or hh20[i] is None:
        return False
    prior = v[i - 5:i]
    return sum(prior) / 5 <= 0.7 * vm[i] and x[i] >= hh20[i]


@reg('vol_price_divergence', 'vol', '量价背离（警示）：收盘创 60 日新高 但 成交量未同步创 60 日新高')
def _s_vol_price_divergence(x, i, C):
    v, vhh, hh60 = C['vol'], C['vhh60'], C['hh60']
    if i < 60 or vhh[i] is None or hh60[i] is None:
        return False
    return x[i] >= hh60[i] and v[i] < vhh[i]


# --- 日内形态 / 跳空（需 OHLC；仅场内涵数可得 → caliber='ohlc'） ---


@reg('gap_up_break', 'ohlc', '跳空高开突破：今日开盘 > 昨日最高 且 收盘高于开盘')
def _s_gap_up_break(x, i, C):
    if i < 2:
        return False
    return C['o'][i] > C['h'][i - 1] and x[i] > C['o'][i]


@reg('gap_down_revert', 'ohlc', '跳空低开回补：今日开盘 < 昨日最低 且 收盘高于开盘')
def _s_gap_down_revert(x, i, C):
    if i < 2:
        return False
    return C['o'][i] < C['l'][i - 1] and x[i] > C['o'][i]


@reg('gap_hold_unfilled', 'ohlc', '跳空缺口未回补：今日开盘 > 昨日最高 且 全天最低仍在昨日最高之上')
def _s_gap_hold_unfilled(x, i, C):
    if i < 2:
        return False
    return C['o'][i] > C['h'][i - 1] and C['l'][i] > C['h'][i - 1]


@reg('hammer_bottom', 'ohlc', '锤子线：下影 ≥ 2×实体、上影 ≤ 实体，且位于 20 日低位区')
def _s_hammer_bottom(x, i, C):
    o, h, l = C['o'][i], C['h'][i], C['l'][i]
    rng = h - l
    if rng <= 0 or C['ll20'][i] is None:
        return False
    body = abs(x[i] - o) or rng * 0.02
    lower = min(x[i], o) - l
    upper = h - max(x[i], o)
    return lower >= 2 * body and upper <= body and x[i] <= C['ll20'][i] * 1.05


@reg('bullish_engulf', 'ohlc', '阳包阴：昨日收阴、今日收阳，且今日实体完整包住昨日实体')
def _s_bullish_engulf(x, i, C):
    if i < 2:
        return False
    o1, o0 = C['o'][i - 1], C['o'][i]
    return x[i - 1] < o1 and x[i] > o0 and x[i] >= o1 and o0 <= x[i - 1]


@reg('morning_star', 'ohlc', '早晨之星：阴线 + 跳空小实体 + 阳线收回首根实体中点之上')
def _s_morning_star(x, i, C):
    if i < 3:
        return False
    o2, o1, o0 = C['o'][i - 2], C['o'][i - 1], C['o'][i]
    if x[i - 2] >= o2:
        return False
    if abs(x[i - 1] - o1) > (o2 - x[i - 2]) * 0.5 or x[i - 1] > x[i - 2]:
        return False
    return x[i] > o0 and x[i] > (o2 + x[i - 2]) / 2


@reg('three_white_soldiers', 'ohlc', '红三兵：连续三根阳线且收盘逐日抬高')
def _s_three_white_soldiers(x, i, C):
    if i < 3:
        return False
    for k in (i - 2, i - 1, i):
        if x[k] <= C['o'][k]:
            return False
    return x[i] > x[i - 1] > x[i - 2]


@reg('dmi_gold', 'ohlc', '+DI 上穿 −DI 且 ADX 向上（趋势启动）')
def _s_dmi_gold(x, i, C):
    p, m, a = C['pdi'], C['mdi'], C['adx']
    if i < 16:
        return False
    if None in (p[i], m[i], a[i], p[i - 1], m[i - 1], a[i - 3]):
        return False
    return _cross(p, m, i) and a[i] > a[i - 3]


@reg('w_bottom', 'ohlc', 'W 底（简化）：60 日内两次探底且第二次不破前低，今日收盘突破颈线')
def _s_w_bottom(x, i, C):
    if i < 60:
        return False
    seg = x[i - 59:i + 1]
    a = seg[:30]
    m1 = min(a)
    p1 = a.index(m1)
    b = seg[p1 + 10:50]
    if len(b) < 10:
        return False
    m2 = min(b)
    if not (m1 * 0.97 <= m2 <= m1 * 1.05):
        return False
    p2 = p1 + 10 + b.index(m2)
    return x[i] > max(seg[p1:p2 + 1])


@reg('box_breakout_retest', 'ohlc', '箱体突破后回踩不破：10 日内曾突破箱顶，今日最低触及箱顶但收盘收回其上')
def _s_box_breakout_retest(x, i, C):
    if i < 70:
        return False
    box_hi = max(x[i - 60:i - 10])
    if not any(x[j] > box_hi for j in range(i - 10, i)):
        return False
    return C['l'][i] <= box_hi < x[i]


# --- 明确不可判定（依赖成交量 / 日内 OHLC / 资金流 / 基本面 等） ---
NOT_JUDGEABLE: dict[str, str] = {
    # 形态主观性高，程序判定不可靠（不硬凑）
    'head_shoulder_bottom': '形态主观性高（头肩底），程序判定不可靠',
    'wedge_breakout': '形态主观性高（楔形），程序判定不可靠',
    # 涨跌停口径：公募基金净值/场内核价无涨跌停制度
    'consecutive_limit_up': '需要涨停阈值；公募基金无涨跌停制度',
    'first_limit_up': '需要涨停判定；公募基金无涨跌停制度',
    # 风控/过滤/仓位叠加规则：不是选基信号
    'atr_trailing_stop': '风控叠加规则，非入场信号',
    'fixed_stop_loss': '风控叠加规则，非入场信号',
    'trailing_drawdown_stop': '风控叠加规则，非入场信号',
    'time_stop_20d': '风控叠加规则，非入场信号',
    'hist_vol_filter': '过滤/叠加规则，非入场信号',
    'vol_target_sizing': '口径受限（仓位规则）',
    'kelly_fraction_sizing': '口径受限（仓位规则）',
    'monthly_rebalance': '口径受限（组合再平衡）',
    # 指数择时开关：不是选基信号
    'index_ma20_timing': '个股择时开关，非选基信号',
    'index_ma250_timing': '个股择时开关，非选基信号',
    'breadth_timing': '需要池内横截面宽度',
    'vol_regime_timing': '指数择时开关，非选基信号',
    # 组合层：需要横截面排序 / 基准超额
    'dual_momentum': '需要横截面排序（组合层）',
    'relative_strength_20': '需要基准指数超额口径',
    # 需要外部数据源
    'main_fund_inflow': '需要资金流向数据',
    'northbound_increase': '需要北向持股数据',
    'margin_buy_surge': '需要融资余额数据',
    'lhb_institution_buy': '需要龙虎榜机构席位数据',
    'pe_band_low': '需要估值分位（PE）',
    'peg_growth': '需要盈利增速（PEG）',
    'roe_stable_high': '需要财务 ROE',
    'earnings_surprise': '需要业绩预告',
    'sector_rotation': '需要行业指数',
    'index_rebalance_event': '需要指数成分调整公告',
}


def build_ctx(xs: list[float], ohlc: list[tuple] | None = None) -> dict:
    """指标上下文。

    `ohlc` 形如 [(date,o,h,l,c,v)]：
      · 传了（场内涵数可得）→ 用真实日内高低 + 成交量，CCI/KDJ/威廉/ATR/DMI 与原规则等价；
      · 没传（场外基金只有净值序列）→ 退化为以收盘价近似，并在口径上如实标注。
    """
    has_ohlc = bool(ohlc) and len(ohlc) == len(xs)
    hi = [r[2] for r in ohlc] if has_ohlc else None
    lo = [r[3] for r in ohlc] if has_ohlc else None
    vol = [r[5] for r in ohlc] if has_ohlc else None
    hh_src = hi if has_ohlc else xs
    ll_src = lo if has_ohlc else xs

    dif, dea, hist = macd(xs)
    k, d, j = kdj_approx(xs, hi=hi, lo=lo)
    bw: list[float | None] = [None] * len(xs)
    mid, up, lo_b = sma(xs, 20), [None] * len(xs), [None] * len(xs)
    sd20 = stdev(xs, 20)
    for i in range(len(xs)):
        if mid[i] is not None and sd20[i] is not None:
            up[i], lo_b[i] = mid[i] + 2 * sd20[i], mid[i] - 2 * sd20[i]
            if mid[i]:
                bw[i] = (up[i] - lo_b[i]) / mid[i]

    ctx = {
        'ma5': sma(xs, 5), 'ma6': sma(xs, 6), 'ma10': sma(xs, 10), 'ma20': sma(xs, 20),
        'ma30': sma(xs, 30), 'ma60': sma(xs, 60), 'ma250': sma(xs, 250),
        'ema12': ema(xs, 12), 'ema26': ema(xs, 26), 'ema20': ema(xs, 20), 'ema50': ema(xs, 50),
        'rsi2': rsi(xs, 2), 'rsi6': rsi(xs, 6), 'rsi12': rsi(xs, 12),
        'dif': dif, 'dea': dea, 'hist': hist,
        'k': k, 'd': d, 'j': j,
        'cci': cci_approx(xs, hi=hi, lo=lo), 'wr': williams_approx(xs, hi=hi, lo=lo),
        'boll_mid': mid, 'boll_up': up, 'boll_lo': lo_b, 'bw': bw, 'sd20': sd20,
        'hh10': roll_max(hh_src, 10), 'hh20': roll_max(hh_src, 20), 'hh55': roll_max(hh_src, 55),
        'hh60': roll_max(hh_src, 60), 'hh120': roll_max(hh_src, 120),
        'll10': roll_min(ll_src, 10), 'll20': roll_min(ll_src, 20), 'll30': roll_min(ll_src, 30),
        'tr': true_range(xs, hi, lo), 'atr14': atr_approx(xs, hi=hi, lo=lo),
    }
    if has_ohlc:
        ob = obv(xs, vol)
        p, m, a = dmi(xs, hi, lo)
        ctx.update({
            'o': [r[1] for r in ohlc], 'h': hi, 'l': lo, 'vol': vol,
            'vma5': sma(vol, 5), 'vma10': sma(vol, 10), 'vma20': sma(vol, 20),
            'obv': ob, 'obv_ma': sma(ob, 30), 'obv_hh60': roll_max(ob, 60),
            'vhh60': roll_max(vol, 60),
            'pdi': p, 'mdi': m, 'adx': a,
        })
    return ctx


def effective_caliber(base: str, has_ohlc: bool):
    """基准口径 → 本次实际口径。返回 None 表示这批数据判定不了。

    · exact  仅需收盘序列 → 恒定 exact
    · approx 原需日内高低（已用收盘极值近似）→ 有 OHLC 即升级为 exact
    · ohlc   必须有日内 OHLC（跳空/形态/DMI）→ 无则不可判定
    · vol    必须有成交量（OBV/放量）→ 无则不可判定
    """
    if base == 'exact':
        return 'exact'
    if base == 'approx':
        return 'exact' if has_ohlc else 'approx'
    if base in ('ohlc', 'vol'):
        return 'exact' if has_ohlc else None
    return None


def scan_one(key: str, xs: list[float], ctx: dict, dates: list[str], has_ohlc: bool = True):
    """返回 (signal_index, caliber, note) 或 None。只看最近 RECENT_DAYS 根。"""
    entry = BUY_SIGNALS.get(key)
    if not entry or len(xs) < 40:
        return None
    base, note = entry
    caliber = effective_caliber(base, has_ohlc)
    if caliber is None:
        return None
    fn = SIGNAL_FNS.get(key)
    if fn is None:
        return None
    start = max(1, len(xs) - RECENT_DAYS)
    for i in range(len(xs) - 1, start - 1, -1):
        try:
            if fn(xs, i, ctx):
                return i, caliber, note
        except Exception:  # noqa: BLE001
            return None
    return None


# ---------------------------------------------------------------------------
# 5) 落库
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS fund_strategy_signal (
  strategy_key TEXT NOT NULL,
  fund_code    TEXT NOT NULL,
  fund_name    TEXT,
  fund_type    TEXT,
  market       TEXT,      -- 场内 / 场外（场内才有成交量）
  signal_date  TEXT,
  days_ago     INTEGER,
  ret_since    REAL,      -- 信号日至今 %
  ret_20d      REAL,      -- 最新 20 日 %
  ret_120d     REAL,      -- 近半年 %（≈122 交易日）
  ret_1y       REAL,      -- 近一年 %（≈244 交易日；场外为累计净值口径，不含分红再投，偏低）
  nav_latest   REAL,      -- 场外=累计净值；场内=收盘价
  nav_date     TEXT,
  bars         INTEGER,
  caliber      TEXT,      -- exact | approx
  caliber_note TEXT,
  source       TEXT,
  computed_at  TEXT,
  PRIMARY KEY (strategy_key, fund_code)
);
CREATE INDEX IF NOT EXISTS idx_fss_key ON fund_strategy_signal(strategy_key);
CREATE TABLE IF NOT EXISTS fund_strategy_meta (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  generated_at TEXT,
  pool_n       INTEGER,
  pool_note    TEXT,
  nav_bars     INTEGER,
  nav_end      TEXT,
  nav_basis    TEXT,
  sources_json TEXT,
  coverage_json TEXT
);
"""


def save(docs, meta, con):
    con.executescript(DDL)
    cur = con.cursor()
    # 旧表迁移：补 market / ret_120d 列（已存在则忽略）
    for col in ('market TEXT', 'ret_120d REAL'):
        try:
            cur.execute('ALTER TABLE fund_strategy_signal ADD COLUMN ' + col)
        except Exception as e:  # noqa: BLE001 —— P2-02：精确吞「列已存在」，其余异常照常抛出
            if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                pass
            else:
                raise
    cur.execute('DELETE FROM fund_strategy_signal')
    cur.executemany(
        'INSERT OR REPLACE INTO fund_strategy_signal (strategy_key,fund_code,fund_name,fund_type,'
        'market,signal_date,days_ago,ret_since,ret_20d,ret_120d,ret_1y,nav_latest,nav_date,bars,'
        'caliber,caliber_note,source,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        docs)
    cur.execute('DELETE FROM fund_strategy_meta')
    cur.execute('INSERT INTO fund_strategy_meta (id,generated_at,pool_n,pool_note,nav_bars,nav_end,'
                'nav_basis,sources_json,coverage_json) VALUES (1,?,?,?,?,?,?,?,?)',
                (meta['generated_at'], meta['pool_n'], meta['pool_note'], meta['nav_bars'],
                 meta['nav_end'], meta['nav_basis'],
                 json.dumps(meta['sources'], ensure_ascii=False),
                 json.dumps(meta['coverage'], ensure_ascii=False)))
    con.commit()


# ---------------------------------------------------------------------------


def _load_cache(path: str, today: str, refresh: bool) -> dict:
    """读取当日缓存（非当日则视为空）。"""
    if refresh or not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            cache = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    if cache.get('__date__') != today:
        return {}
    return {k: v for k, v in cache.items() if not k.startswith('__')}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pool-per-type', type=int, default=200,
                    help='每类场外基金取前 N（近一年收益降序）；0=不取场外')
    ap.add_argument('--inner-per-node', type=int, default=300,
                    help='场内 ETF / LOF 各取前 N（按成交额降序，唯一带成交量）；0=不取场内')
    ap.add_argument('--bars', type=int, default=260, help='每只标的多取少根序列')
    ap.add_argument('--throttle', type=float, default=0.12, help='请求间隔秒')
    ap.add_argument('--limit', type=int, default=0, help='调试：只抓前 N 只标的')
    ap.add_argument('--refresh', action='store_true', help='忽略当日缓存')
    args = ap.parse_args()

    today = datetime.now().strftime('%Y-%m-%d')
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ---- 备份 app.db（项目惯例） ----
    if os.path.exists(DB_PATH):
        bak = DB_PATH + '.bak.fundlink-' + datetime.now().strftime('%Y%m%d-%H%M%S')
        with open(DB_PATH, 'rb') as f, open(bak, 'wb') as g:
            g.write(f.read())
        print('[0/5] 已备份 %s' % os.path.basename(bak), flush=True)

    # ---- 1) 双池：场外公募基金 + 场内涵数（ETF/LOF，唯一有成交量） ----
    print('[1/5] 拉取候选池 …', flush=True)
    pool: list[dict] = []
    if args.pool_per_type > 0:
        pool += fetch_pool(args.pool_per_type, args.throttle)
    if args.inner_per_node > 0:
        pool += fetch_inner_pool(args.inner_per_node, args.throttle)
    if args.limit:
        pool = pool[:args.limit]
    n_in = sum(1 for p in pool if p.get('market') == '场内')
    print('      池合计 %d 只（场外 %d / 场内 %d）' % (len(pool), len(pool) - n_in, n_in),
          flush=True)

    # ---- 2) 序列：场外→累计净值；场内→日线 OHLCV（各带当日缓存） ----
    navs = _load_cache(CACHE_PATH, today, args.refresh)
    ohlcs = _load_cache(CACHE_OHLC_PATH, today, args.refresh)
    nav_miss = [p for p in pool if p.get('market') != '场内' and p['code'] not in navs]
    inn_miss = [p for p in pool if p.get('market') == '场内' and p['code'] not in ohlcs]
    print('[2/5] 抓取序列：场外净值 缓存 %d 待抓 %d ｜ 场内日线 缓存 %d 待抓 %d'
          % (len(navs), len(nav_miss), len(ohlcs), len(inn_miss)), flush=True)
    for n, p in enumerate(nav_miss, 1):
        try:
            rows = fetch_nav(p['code'], args.bars, args.throttle)
            if rows:
                navs[p['code']] = rows
        except Exception as e:  # noqa: BLE001
            print('      ! %s %s: %s' % (p['code'], p['name'][:12], str(e)[:70]), flush=True)
        if n % 50 == 0:
            print('      … 净值 %d/%d' % (n, len(nav_miss)), flush=True)
    for n, p in enumerate(inn_miss, 1):
        try:
            rows = fetch_ohlcv(p.get('sym') or '', args.bars, args.throttle)
            if rows:
                ohlcs[p['code']] = rows
        except Exception as e:  # noqa: BLE001
            print('      ! %s %s: %s' % (p['code'], p['name'][:12], str(e)[:70]), flush=True)
        if n % 50 == 0:
            print('      … 日线 %d/%d' % (n, len(inn_miss)), flush=True)
    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump({'__date__': today, **navs}, f, ensure_ascii=False)
    with open(CACHE_OHLC_PATH, 'w', encoding='utf-8') as f:
        json.dump({'__date__': today, **ohlcs}, f, ensure_ascii=False)

    # ---- 3) 逐策略扫描 ----
    print('[3/5] 策略 × 标的 信号扫描 …', flush=True)
    codes_ctx: dict = {}
    for p in pool:
        code = p['code']
        if p.get('market') == '场内':
            rows = ohlcs.get(code)
            if not rows:
                continue
            dates = [r[0] for r in rows]
            xs = [float(r[4]) for r in rows]
            has_ohlc = True
        else:
            rows = navs.get(code)
            if not rows:
                continue
            dates = [r[0] for r in rows]
            xs = [float(r[1]) for r in rows]
            has_ohlc = False
        if len(xs) < 40:
            continue
        codes_ctx[code] = (p, dates, xs, build_ctx(xs, rows if has_ohlc else None), has_ohlc)
    n_ohlc = sum(1 for v in codes_ctx.values() if v[4])
    print('      可用序列 %d / %d（场外 %d / 场内 %d）'
          % (len(codes_ctx), len(pool), len(codes_ctx) - n_ohlc, n_ohlc), flush=True)
    has_ohlc_any = n_ohlc > 0

    docs, cov = [], {}
    for key, (base, note) in sorted(BUY_SIGNALS.items()):
        best = effective_caliber(base, has_ohlc_any)
        if best is None:
            cov[key] = {'status': 'not_judgeable', 'caliber': None, 'hits': 0,
                        'reason': '需要成交量；本次池内无场内涵数（用 --inner-per-node 补充）'}
            print('      %-26s %-6s 命中 0（本次无场内数据）' % (key, '-'), flush=True)
            continue
        hit = 0
        for code, (p, dates, xs, ctx, has_ohlc) in codes_ctx.items():
            r = scan_one(key, xs, ctx, dates, has_ohlc)
            if not r:
                continue
            i, cal, cnote = r
            last = len(xs) - 1
            ret_since = (xs[last] / xs[i] - 1) * 100 if xs[i] else None
            ret_20d = (xs[last] / xs[last - 20] - 1) * 100 if last >= 20 and xs[last - 20] else None
            ret_120d = None
            k6 = last - 122
            if k6 >= 0 and xs[k6]:
                ret_120d = (xs[last] / xs[k6] - 1) * 100
            ret_1y = None
            k = last - 244
            if k >= 0 and xs[k]:
                ret_1y = (xs[last] / xs[k] - 1) * 100
            docs.append((key, code, p['name'], p.get('type'), p.get('market', '场外'),
                         dates[i], last - i,
                         round(ret_since, 2) if ret_since is not None else None,
                         round(ret_20d, 2) if ret_20d is not None else None,
                         round(ret_120d, 2) if ret_120d is not None else None,
                         round(ret_1y, 2) if ret_1y is not None else None,
                         round(xs[last], 4), dates[last], len(xs), cal, cnote,
                         SRC_KLINE if has_ohlc else SRC_NAV, now))
            hit += 1
        cov[key] = {'status': 'ok', 'caliber': best, 'hits': hit,
                    'scope': ('仅场内涵数（含成交量）' if base in ('vol', 'ohlc') else '全部标的')}
        print('      %-26s %-6s 命中 %d' % (key, best, hit), flush=True)

    for key, why in sorted(NOT_JUDGEABLE.items()):
        cov[key] = {'status': 'not_judgeable', 'caliber': None, 'hits': 0, 'reason': why}

    # 目录里其余 key（未登记）也如实标注
    try:
        sys.path.insert(0, ROOT)
        import strategy_data
        for s in getattr(strategy_data, 'STRATEGIES', []):
            k = s.get('key')
            if k and k not in cov:
                cov[k] = {'status': 'not_judgeable', 'caliber': None, 'hits': 0,
                          'reason': '未纳入净口径判定范围'}
    except Exception as e:  # noqa: BLE001
        print('      ! 目录读取失败：%s' % e, flush=True)

    nav_end = max((codes_ctx[c][1][-1] for c in codes_ctx), default=None)
    meta = {
        'generated_at': now,
        'pool_n': len(codes_ctx),
        'pool_note': ('双池：① 场外公募基金（股票型/混合型/指数型，近一年收益降序各前 %d 只）'
                      '＋ ② 场内涵数 ETF/LOF（按成交额降序各前 %d 只）。'
                      '场外只有净值序列（无成交量），量能与日内形态类策略仅在 ② 中判定。'
                      % (args.pool_per_type, args.inner_per_node)),
        'nav_bars': args.bars,
        'nav_end': nav_end,
        'nav_basis': ('场外＝累计净值序列（不含分红再投，收益偏低）；'
                      '场内＝日线收盘价 + 成交量'),
        'sources': [SRC_RANK, SRC_NAV, SRC_INNER, SRC_KLINE,
                    '本仓库 strategy_data.py（策略规则真源）'],
        'coverage': cov,
    }

    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        save(docs, meta, con)
    finally:
        con.close()
    print('[4/5] 落库完成：fund_strategy_signal %d 行' % len(docs), flush=True)

    keys = ('strategy_key', 'fund_code', 'fund_name', 'fund_type', 'market', 'signal_date',
            'days_ago', 'ret_since', 'ret_20d', 'ret_120d', 'ret_1y', 'nav_latest', 'nav_date',
            'bars', 'caliber', 'caliber_note', 'source', 'computed_at')
    with open(RESULT_PATH, 'w', encoding='utf-8') as f:
        json.dump({'meta': meta, 'rows': [dict(zip(keys, d)) for d in docs]},
                  f, ensure_ascii=False, indent=1)
    n_ok = sum(1 for v in cov.values() if v.get('status') == 'ok')
    print('[5/5] 明细导出 %s' % os.path.basename(RESULT_PATH), flush=True)
    print('完成。可判定策略 %d / %d 条；命中总数 %d。' % (n_ok, len(cov), len(docs)), flush=True)


if __name__ == '__main__':
    main()
