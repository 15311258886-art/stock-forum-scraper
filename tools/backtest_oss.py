#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开源规则（oss_*）多持有期回测引擎。

为什么单独写这一个（不复用 viewer/signal_backtest.py、也不读 stock_indicators_daily）：

  1. **必须按分类给不同的持有期。** 「距 52 周高 25% 内」是 250 日尺度的位置判据，
     拿 T+5 去测它，测不出东西是必然的，不是规则错、是尺子错。
  2. **必须能分段看稳定性。** 3 年整体平均会把「半年有效半年反向」抹成一条好看的零线。
  3. **必须能按市场状态分层。** 否则永远回答不了「市场好就全通过、市场差就全废」。
  4. `stock_indicators_daily` 在 2026-11-02 被主动裁剪，只剩 261 个交易日，用它回测
     等于只看最近一年 —— 所以指标一律从 `viewer/kbase.db` 的后复权日线现算。

数据源（唯一）：`viewer/kbase.db` 表 `kline`，`code,date,o,h,l,c,v,adj`，adj 恒为 'hfq'（后复权）。
后复权的意义：历史价格固定，不会因为将来发生除权而改变，跨年回测的收益才算得对。

口径（README 里那几条硬要求，改之前先读）：
  · 信号日 d 收盘产生信号 → **d+1 收盘建仓** → d+1+H 收盘平仓（A 股 T+1）
  · 超额 = 个股收益 − 同日全市场等权收益（剔除 ST / 次新 / 停牌）− 双边成本 0.25%
  · t 值按**日**聚合后再算（每天一个平均超额），不按个股样本算 —— 后者会把同一只票
    连续几天的涨跌当成独立样本，t 虚高 2~3 倍
  · 另外给一个 Newey-West t（滞后阶 = H），因为持有期 H 天意味着相邻信号日的收益窗口
    重叠了 H−1 天，日聚合只解决了「同日横截面相关」，没解决「跨日自相关」

用法：
    python tools/backtest_oss.py                        # 11 条规则 × 各自分类的持有期
    python tools/backtest_oss.py --grid                 # 再加一张全持有期网格（找错配用）
    python tools/backtest_oss.py --rules oss_reversal20 --since 2024-02-07
    python tools/backtest_oss.py --horizons 5,20        # 强制所有规则都跑这两个持有期

★ 本脚本只读 kbase.db / app.db，不写任何产品库，不改 viewer/ 下任何文件。
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KBASE = ROOT / "viewer" / "kbase.db"
APP_DB = ROOT / "viewer" / "app.db"
LOG_DIR = ROOT / "viewer" / "logs"

try:
    import numpy as np
except ImportError:                                       # pragma: no cover
    sys.exit("需要 numpy：请用 .venv/Scripts/python.exe 运行（项目 .venv 已装 numpy）")

try:
    import pandas as pd
except ImportError:                                       # pragma: no cover
    sys.exit("需要 pandas：请用 .venv/Scripts/python.exe 运行")


# ================================================================
# 口径常量
# ================================================================

COST_PCT = 0.25          # 双边手续费 + 滑点，直接从超额里减（百分点）
MIN_LISTED_BARS = 60     # 上市不满 60 个交易日的次新剔除
WARMUP_BARS = 300        # 信号日之前至少要留这么多交易日给指标预热（250 日高 + EMA 收敛）

# 分类 → 该分类要测的持有期（硬要求，不许自己改）
CATEGORY_HORIZONS: dict[str, tuple[int, ...]] = {
    "t1_surf": (1, 2, 3),
    "short_hot": (3, 5, 10),
    "short_dip": (10, 20, 30),
    "mid_break": (20, 40, 60),
    "mid_base": (60, 90, 120),
    "long_hold": (120, 180, 250),
    "gate": (5, 20),
}

CATEGORY_CN = {
    "t1_surf": "竞价冲浪",
    "short_hot": "短线热点",
    "short_dip": "超跌修复",
    "mid_break": "中线突破",
    "mid_base": "筑底布局",
    "long_hold": "长期埋伏",
    "gate": "跨周期闸门",
}

# 11 条 oss 规则 → 分类（照主理人给的表，不要自己挪）
RULE_CATEGORY: dict[str, str] = {
    "oss_burst_vol": "short_hot",
    "oss_burst": "short_hot",
    "oss_shrink_vol": "short_hot",
    "oss_rsi_overbought": "short_hot",
    "oss_pullback_ma20": "short_dip",
    "oss_mom20": "short_dip",
    "oss_reversal20": "short_dip",
    "oss_ma_align": "mid_break",
    "oss_near_high": "mid_base",
    "oss_near_high_tight": "mid_base",
    "oss_high_vol": "gate",
}

ALL_RULES = tuple(RULE_CATEGORY.keys())

# 全持有期网格（只用于找「持有期错配」，不参与判定）
HORIZON_GRID = (1, 2, 3, 5, 10, 20, 30, 40, 60, 90, 120, 180, 250)

# 6 个半年段（左闭右开，按信号日切）
SEGMENTS = (
    ("2023-11-04", "2024-05-04", "2023-11~2024-05"),
    ("2024-05-04", "2024-11-04", "2024-05~2024-11"),
    ("2024-11-04", "2025-05-04", "2024-11~2025-05"),
    ("2025-05-04", "2025-11-04", "2025-05~2025-11"),
    ("2025-11-04", "2026-05-04", "2025-11~2026-05"),
    ("2026-05-04", "2026-11-04", "2026-05~2026-11"),
)

REGIMES = (
    ("bull", "牛市(60日>+5%)", 5.0, math.inf),
    ("range", "震荡(-5%~+5%)", -5.0, 5.0),
    ("bear", "熊市(60日<-5%)", -math.inf, -5.0),
)


# ================================================================
# 取数
# ================================================================

def load_calendar(con: sqlite3.Connection, d0: str, d1: str) -> list[str]:
    """交易日历（kbase 里出现过的日期，就是这个库真实有行情的日子）。"""
    return [str(r[0]) for r in con.execute(
        "SELECT DISTINCT date FROM kline WHERE date>=? AND date<=? ORDER BY date", (d0, d1))]


def load_st_codes(db: Path) -> set[str]:
    """当前 ST 代码集合。

    ★ 局限（已在报告里写明，不藏）：`stock_names` 存的是**当前**名称，所以这是把
      「今天带 ST」的票在整个回测期里都剔掉。真正严格的做法需要带日期的历史名称表，
      库里没有。影响方向：会少剔一批「当年 ST、现在摘帽」的票（它们的极端反弹不进样本），
      也会多剔一批「当年正常、现在 ST」的票（这批恰是长期最差的），两者不完全抵消，
      但对**规则之间**的相对比较影响有限（11 条规则吃的是同一份剔除）。
    """
    con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    try:
        return {str(r[0]) for r in con.execute(
            "SELECT code FROM stock_names WHERE upper(name) LIKE '%ST%'")}
    except sqlite3.Error:
        return set()
    finally:
        con.close()


def iter_bars(con: sqlite3.Connection, d0: str, d1: str):
    """按 code 分组流式产出 (code, c, h, l, v, gidx)。

    流式（游标迭代 + 一次只压一只票）而不是 fetchall：区间内约 550 万行，
    一次性 fetchall 会吃掉 500MB+ 内存，而这里峰值只跟单票行数有关。
    """
    cur = con.execute(
        "SELECT code,date,c,h,l,v FROM kline"
        " WHERE date>=? AND date<=? AND c>0 ORDER BY code,date", (d0, d1))
    cur_code = None
    c_buf: list[float] = []
    h_buf: list[float] = []
    l_buf: list[float] = []
    v_buf: list[float] = []
    d_buf: list[str] = []
    for code, date, c, h, l, v in cur:
        if code != cur_code:
            if cur_code is not None:
                yield cur_code, c_buf, h_buf, l_buf, v_buf, d_buf
            cur_code = str(code)
            c_buf, h_buf, l_buf, v_buf, d_buf = [], [], [], [], []
        c_buf.append(float(c))
        h_buf.append(float(h) if h is not None else float(c))
        l_buf.append(float(l) if l is not None else float(c))
        v_buf.append(float(v) if v is not None else 0.0)
        d_buf.append(str(date))
    if cur_code is not None:
        yield cur_code, c_buf, h_buf, l_buf, v_buf, d_buf


# ================================================================
# 判据字段（口径逐字对齐 viewer/indicators.py + toolbox._build_oss_snapshot）
# ================================================================

def _pct_arr(cur: np.ndarray, prev: np.ndarray) -> np.ndarray:
    """涨跌幅（%）。prev/cur 非正或缺失 → NaN（等价 toolbox._pct 返回 None）。"""
    out = np.full(cur.shape, np.nan, dtype=np.float64)
    ok = np.isfinite(cur) & np.isfinite(prev) & (cur > 0) & (prev > 0)
    np.divide(cur, prev, out=out, where=ok)
    out[ok] = (out[ok] - 1.0) * 100.0
    return out


def stock_features(c: np.ndarray, h: np.ndarray, l: np.ndarray, v: np.ndarray) -> dict:
    """单只票在**每个本地 bar** 上的判据字段。

    字段定义（与产品里跑的那一套严格同源，不要「顺手改成正确的 N 日」）：
      · ma5/20/60   = 收盘的简单移动平均（indicators.py: c.rolling(n).mean()）
      · rsi6        = Wilder 平滑（alpha=1/6, adjust=False）
      · atr14       = Wilder 平滑（alpha=1/14, adjust=False），atr_pct = atr/close*100
      · volume_ratio= v / v 的 5 日均（含当日，indicators.py 原样）
      · ret1        = c / c[-1] − 1
      · ret2        = c / c[-2] − 1
      · ret5        = c / c[-6] − 1   ← ⚠️ 刻意的「差一根」：toolbox 的 deque[-7] 就是 6 根前
      · mom20       = c / c[-21] − 1  ← ⚠️ 同上（deque[-22]）
      · dist_high250= c / 最近 251 根最高收盘 − 1（历史不足 251 根时用已知的最高）
      · ma20_dev    = c / ma20 − 1
    """
    s = pd.Series(c, dtype="float64")
    ma5 = s.rolling(5).mean().to_numpy()
    ma20 = s.rolling(20).mean().to_numpy()
    ma60 = s.rolling(60).mean().to_numpy()

    # RSI6（Wilder）
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.ewm(alpha=1.0 / 6, adjust=False).mean()
    al = loss.ewm(alpha=1.0 / 6, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    rsi6 = (100.0 - 100.0 / (1.0 + rs)).to_numpy()

    # ATR14（Wilder）
    hs = pd.Series(h, dtype="float64")
    ls = pd.Series(l, dtype="float64")
    pc = s.shift(1)
    tr = pd.concat([(hs - ls), (hs - pc).abs(), (pc - ls).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / 14, adjust=False).mean().to_numpy()

    vs = pd.Series(v, dtype="float64")
    vol_ma5 = vs.rolling(5).mean().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        vr = np.where(vol_ma5 > 0, v / vol_ma5, np.nan)

    def _shift(n: int) -> np.ndarray:
        out = np.full(c.shape, np.nan, dtype=np.float64)
        if c.size > n:
            out[n:] = c[:-n]
        return out

    ret1 = _pct_arr(c, _shift(1))
    ret2 = _pct_arr(c, _shift(2))
    ret5 = _pct_arr(c, _shift(6))
    mom20 = _pct_arr(c, _shift(21))

    # 251 根（含当日）最高收盘 —— 对应 toolbox 那个 251 天滚动窗口里的 running max
    high251 = pd.Series(c, dtype="float64").rolling(251, min_periods=1).max().to_numpy()
    dist_high250 = _pct_arr(c, high251)

    ma20_dev = _pct_arr(c, ma20)

    return {
        "close": c, "ma5": ma5, "ma20": ma20, "ma60": ma60, "rsi6": rsi6,
        "atr_pct": np.where(c > 0, atr / np.where(c > 0, c, np.nan) * 100.0, np.nan),
        "volume_ratio": vr, "ret1": ret1, "ret2": ret2, "ret5": ret5,
        "mom20": mom20, "dist_high250": dist_high250, "ma20_dev": ma20_dev,
    }


def _f_pos(x: np.ndarray) -> np.ndarray:
    """toolbox._f 的数组版：非正 / 非数一律当「没有」。"""
    out = np.asarray(x, dtype=np.float64)
    return np.where(np.isfinite(out) & (out > 0), out, np.nan)


def rule_hits(feat: dict, rule: str) -> np.ndarray:
    """一条规则在每根 bar 上是否命中（bool 数组）。

    逐条照抄 `toolbox._p_*` 的默认参数与判断顺序（默认值取自 tool 注册表的
    `default_params`，也就是**原作档**参数）。凡是「字段缺失」的分支一律判不命中，
    与产品行为一致 —— 产品里那种票会被淘汰并写明原因。
    """
    c = feat["close"]
    f = _f_pos

    if rule == "oss_ma_align":
        ma5, ma20, ma60 = f(feat["ma5"]), f(feat["ma20"]), f(feat["ma60"])
        ok = np.isfinite(ma5) & np.isfinite(ma20) & np.isfinite(ma60)
        return ok & (c > ma5) & (ma5 > ma20) & (ma20 > ma60)

    if rule in ("oss_near_high", "oss_near_high_tight"):
        gap = 25.0 if rule == "oss_near_high" else 5.0
        d = feat["dist_high250"]
        return np.isfinite(d) & (d >= -gap)

    if rule == "oss_burst_vol":                      # min_ret=4, min_vr=1.5
        r1, vr = feat["ret1"], f(feat["volume_ratio"])
        return np.isfinite(r1) & np.isfinite(vr) & (r1 >= 4.0) & (vr >= 1.5)

    if rule == "oss_burst":                          # min_ret=4
        r1 = feat["ret1"]
        return np.isfinite(r1) & (r1 >= 4.0)

    if rule == "oss_pullback_ma20":                  # min_dev=-3, max_dev=0
        d = feat["ma20_dev"]
        return np.isfinite(d) & (d >= -3.0) & (d <= 0.0)

    if rule == "oss_shrink_vol":                     # max_vr=0.7
        vr = f(feat["volume_ratio"])
        return np.isfinite(vr) & (vr <= 0.7)

    if rule == "oss_mom20":                          # win=mom20, min_mom=20
        m = feat["mom20"]
        return np.isfinite(m) & (m >= 20.0)

    if rule == "oss_reversal20":                     # win=mom20, max_mom=-20
        m = feat["mom20"]
        return np.isfinite(m) & (m <= -20.0)

    if rule == "oss_high_vol":                       # min_atr=6
        a = f(feat["atr_pct"])
        return np.isfinite(a) & (a >= 6.0)

    if rule == "oss_rsi_overbought":                 # min_rsi=80
        r = f(feat["rsi6"])
        return np.isfinite(r) & (r >= 80.0)

    raise KeyError("未登记的规则：%s" % rule)


# ================================================================
# 统计工具
# ================================================================

def tstat(x: np.ndarray) -> float:
    """普通 t 值（对日序列）。"""
    x = x[np.isfinite(x)]
    n = x.size
    if n < 3:
        return float("nan")
    sd = x.std(ddof=1)
    if not np.isfinite(sd) or sd <= 0:
        return float("nan")
    return float(x.mean() / (sd / math.sqrt(n)))


def nw_tstat(x: np.ndarray, lag: int) -> float:
    """Newey-West t 值（Bartlett 核，滞后阶 = 持有期）。

    为什么需要它：持有 H 天意味着相邻两天的收益窗口重叠 H−1 天，日序列本身强自相关。
    普通 t 只处理了「同一天多只票」的横截面相关，没处理这条，H 越大虚高越多。
    """
    x = x[np.isfinite(x)]
    n = x.size
    if n < 5:
        return float("nan")
    m = float(x.mean())
    d = x - m
    lag = max(1, min(int(lag), n - 2))
    var = float((d * d).sum() / n)
    for k in range(1, lag + 1):
        gk = float((d[k:] * d[:-k]).sum() / n)
        var += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    if var <= 0:
        return float("nan")
    return m / math.sqrt(var / n)


def daily_agg(excess: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """按日聚合：(每天的平均超额, 每天命中只数)。NaN 表示当天没有有效样本。"""
    m = mask & np.isfinite(excess)
    cnt = m.sum(axis=0)
    tot = np.where(m, excess, 0.0).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        daily = np.where(cnt > 0, tot / np.maximum(cnt, 1), np.nan)
    return daily, cnt


def _block(daily: np.ndarray, cnt: np.ndarray, excess: np.ndarray, mask: np.ndarray,
           lag: int, ret: np.ndarray | None = None,
           bench: np.ndarray | None = None) -> dict:
    """一段（整体 / 某半年段 / 某市场状态）的统计块。"""
    ok = np.isfinite(daily)
    n_days = int(ok.sum())
    n_samples = int((mask & np.isfinite(excess)).sum())
    if n_days == 0 or n_samples == 0:
        return {"n_days": 0, "n_samples": 0, "mean_excess": None, "t": None,
                "t_nw": None, "win_days": None, "win_samples": None, "avg_hits": None,
                "mean_ret": None, "mean_bench": None,
                "mean_bench_sample": None}
    d = daily[ok]
    m = mask & np.isfinite(excess)
    ex = excess[m]
    t_val = tstat(d)
    nw_val = nw_tstat(d, lag)
    out = {
        "n_days": n_days,
        "n_samples": n_samples,
        "mean_excess": round(float(d.mean()), 4),
        "t": round(t_val, 3) if np.isfinite(t_val) else None,
        "t_nw": round(nw_val, 3) if np.isfinite(nw_val) else None,
        "win_days": round(float((d > 0).mean()) * 100.0, 2),
        "win_samples": round(float((ex > 0).mean()) * 100.0, 2),
        "avg_hits": round(n_samples / max(1, n_days), 2),
        "mean_ret": round(float(ret[m].mean()), 4) if ret is not None else None,
        "mean_bench": None,
    }
    if bench is not None:
        bd = bench[ok]
        bd = bd[np.isfinite(bd)]
        out["mean_bench"] = round(float(bd.mean()), 4) if bd.size else None
        # 样本加权（每个样本所在那一天的基准）—— 与 mean_ret 相减才对得上超额，
        # 而 mean_bench 是「按天等权」，两个数在命中数随行情剧烈变化时差得很远，
        # 两个都留着，免得被人拿错那个去对账。
        bs = np.nanmean(np.where(m, bench[None, :], np.nan))
        out["mean_bench_sample"] = round(float(bs), 4) if np.isfinite(bs) else None
    return out


# ================================================================
# 主回测
# ================================================================

def run(since: str, until: str, rules: tuple[str, ...], horizons_mode: str,
        verbose: bool = True) -> dict:
    t_start = time.time()
    timings: dict[str, float] = {}

    if not KBASE.exists():
        sys.exit("找不到后复权日线库：%s" % KBASE)

    con = sqlite3.connect("file:%s?mode=ro" % KBASE.as_posix(), uri=True)
    cal_all = load_calendar(con, "1990-01-01", until)
    if not cal_all:
        sys.exit("kbase.kline 在 %s 之前没有行情" % until)
    until = cal_all[-1]                                   # 落到真实最后交易日

    sig_start_idx = next((i for i, d in enumerate(cal_all) if d >= since), None)
    if sig_start_idx is None:
        sys.exit("窗口起点 %s 晚于库内最后交易日 %s" % (since, until))
    # 预热窗口：信号日之前再多给 WARMUP_BARS 个交易日
    warm_idx = max(0, sig_start_idx - WARMUP_BARS)
    d0 = cal_all[warm_idx]
    cal = cal_all[warm_idx:]
    n_cal = len(cal)
    sig_start = sig_start_idx - warm_idx                  # 信号日在 cal 里的起始下标
    dates = cal

    if verbose:
        print("[1/5] 日历：预热起 %s → 信号起 %s → 终点 %s（%d 个交易日，其中信号日 %d 个）"
              % (d0, dates[sig_start], until, n_cal, n_cal - sig_start), flush=True)

    st_codes = load_st_codes(APP_DB)

    # ---------- 持有期集合 ----------
    if horizons_mode == "grid":
        need_h = sorted(set(HORIZON_GRID))
    elif horizons_mode == "custom":
        need_h = sorted(set(_CUSTOM_HORIZONS))
    else:
        need_h = sorted({h for r in rules for h in CATEGORY_HORIZONS[RULE_CATEGORY[r]]})
    max_h = max(need_h)

    # ---------- 2. 逐票算判据 ----------
    t0 = time.time()
    hit_rows: dict[str, list[np.ndarray]] = {r: [] for r in rules}
    code_list: list[str] = []
    close_cols: list[np.ndarray] = []
    gidx_cols: list[np.ndarray] = []
    n_sig = n_cal - sig_start
    cal_map = {d: i for i, d in enumerate(dates)}

    for code, cb, hb, lb, vb, db in iter_bars(con, d0, until):
        c = np.asarray(cb, dtype=np.float64)
        h = np.asarray(hb, dtype=np.float64)
        l = np.asarray(lb, dtype=np.float64)
        v = np.asarray(vb, dtype=np.float64)
        if c.size < MIN_LISTED_BARS:
            continue
        gidx = np.array([cal_map.get(d, -1) for d in db], dtype=np.int64)
        keep = gidx >= 0
        if not keep.any():
            continue
        gidx = gidx[keep]
        if gidx.size < MIN_LISTED_BARS:
            continue
        feat = stock_features(c[keep], h[keep], l[keep], v[keep])
        code_list.append(code)
        close_cols.append(c[keep])
        gidx_cols.append(gidx)
        sig_mask = gidx >= sig_start
        sig_pos = gidx[sig_mask] - sig_start      # 信号日在信号窗口里的位置
        for r in rules:
            col = np.zeros(n_sig, dtype=bool)
            col[sig_pos] = rule_hits(feat, r)[sig_mask]
            hit_rows[r].append(col)
    con.close()

    n_stock = len(code_list)
    hits = {r: (np.vstack(hit_rows[r]) if hit_rows[r]
                else np.zeros((n_stock, n_sig), dtype=bool)) for r in rules}
    if n_stock == 0:
        sys.exit("没有可用的票（窗口 %s~%s 内无行情）" % (d0, until))
    timings["指标计算"] = round(time.time() - t0, 1)
    if verbose:
        print("[2/5] 判据：%d 只票 × %d 个信号日（用时 %.1fs）"
              % (n_stock, n_sig, timings["指标计算"]), flush=True)

    # ---------- 3. 对齐矩阵 ----------
    t0 = time.time()
    mat = np.full((n_stock, n_cal), np.nan, dtype=np.float32)
    for s in range(n_stock):
        mat[s, gidx_cols[s]] = close_cols[s].astype(np.float32)
    present = np.isfinite(mat)
    # 上市交易日数（含当日）：用来剔次新
    bars_upto = np.cumsum(present, axis=1, dtype=np.int32)
    st_vec = np.array([c in st_codes for c in code_list], dtype=bool)
    # 收盘前向填充：只用于**平仓端**（H 日内停牌就顺延到复牌那天卖）
    idx = np.where(present, np.arange(n_cal)[None, :], 0)
    np.maximum.accumulate(idx, axis=1, out=idx)
    filled = np.take_along_axis(mat, idx, axis=1)
    timings["对齐矩阵"] = round(time.time() - t0, 1)

    # ---------- 4. 市场状态：全市场等权 60 日涨跌 ----------
    t0 = time.time()
    # 基准腿：非 ST、上市满 60 个交易日、当日有行情
    uni = present & (~st_vec)[:, None] & (bars_upto >= MIN_LISTED_BARS)
    mkt60 = np.full(n_cal, np.nan, dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        for i in range(60, n_cal):
            a = mat[:, i]
            b = mat[:, i - 60]
            ok = np.isfinite(a) & np.isfinite(b) & (b > 0) & uni[:, i]
            if ok.sum() >= 100:
                mkt60[i] = float(((a[ok] / b[ok] - 1.0) * 100.0).mean())
    timings["市场状态"] = round(time.time() - t0, 1)

    # ---------- 5. 逐个持有期算超额 + 统计 ----------
    t0 = time.time()
    results = []
    for H in need_h:
        ts = np.arange(sig_start, n_cal - H - 1)        # 信号日在 cal 中的下标
        ts_sig = ts - sig_start                          # 同一批信号日在 hits 里的列号
        if ts.size < 20:
            if verbose:
                print("  T+%d：窗口不够（只剩 %d 个可测信号日），跳过" % (H, ts.size))
            continue
        entry = mat[:, ts + 1]                           # 建仓价（d+1 收盘）
        exitp = filled[:, ts + 1 + H]                    # 平仓价（d+1+H 收盘，停牌顺延）
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = (exitp / entry - 1.0) * 100.0
        ret = np.where(np.isfinite(ret), ret, np.nan)
        base = uni[:, ts] & present[:, ts + 1] & np.isfinite(ret)        # 全市场等权收益（同一天、同一持有期、同一批可交易的票）
        cnt_b = base.sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            bench = np.where(cnt_b > 0,
                             np.where(base, ret, 0.0).sum(axis=0) / np.maximum(cnt_b, 1),
                             np.nan)
        excess = ret - bench[None, :] - COST_PCT
        # 建仓日涨停占比（诚实标注用：涨停当天买不进去，样本会偏乐观）
        with np.errstate(invalid="ignore", divide="ignore"):
            entry_ret = (mat[:, ts + 1] / mat[:, ts] - 1.0) * 100.0
        lu_thr = np.array([_limit_up_thr(c) for c in code_list])[:, None]
        limitup = np.isfinite(entry_ret) & (entry_ret >= lu_thr)
        sig_dates = np.array([dates[int(i)] for i in ts])

        for r in rules:
            hit = hits[r][:, ts_sig] & base
            daily, cnt = daily_agg(excess, hit)
            blk = _block(daily, cnt, excess, hit, H, ret=ret, bench=bench)
            if blk["n_samples"] == 0:
                continue
            # 建仓日几乎封板的占比
            m_hit = hit & np.isfinite(excess)
            pct_lu = round(float(limitup[m_hit].mean()) * 100.0, 2) if m_hit.sum() else None
            # 建仓日缺口：信号日收盘 → 次日收盘。这是「收盘出信号、次日收盘买」必须付的
            # 追价成本，也是本口径与你把信号当天收盘价当买价时最大的差异来源。
            gap_ok = m_hit & np.isfinite(entry_ret)
            entry_gap = round(float(entry_ret[gap_ok].mean()), 3) if gap_ok.sum() else None

            # --- 分段稳定性 ---
            segs = []
            for s0, s1, label in SEGMENTS:
                sel = (sig_dates >= s0) & (sig_dates < s1)
                if not sel.any():
                    segs.append({"label": label, "n_days": 0, "n_samples": 0,
                                 "mean_excess": None, "t": None, "dir": "无样本"})
                    continue
                sb = _block(daily[sel], cnt[sel], excess[:, sel], hit[:, sel], H,
                            ret=ret[:, sel], bench=bench[sel])
                if sb["n_samples"] == 0:
                    segs.append({"label": label, "n_days": 0, "n_samples": 0,
                                 "mean_excess": None, "t": None, "dir": "无样本"})
                    continue
                me = sb["mean_excess"]
                sb.update({"label": label,
                           "dir": "+" if me > 0 else ("-" if me < 0 else "0")})
                segs.append(sb)

            overall_sign = 1 if (blk["mean_excess"] or 0) > 0 else -1
            n_same = 0
            n_opp_sig = 0
            for sg in segs:
                if sg["mean_excess"] is None:
                    continue
                sgn = 1 if sg["mean_excess"] > 0 else -1
                if sgn == overall_sign:
                    n_same += 1
                elif sg["t"] is not None and abs(sg["t"]) > 2.0:
                    n_opp_sig += 1
            n_seg_ok = sum(1 for sg in segs if sg["mean_excess"] is not None)
            stable = (n_same >= 4 and n_opp_sig <= 1)

            # --- 市场状态分层 ---
            reg = {}
            for key, label, lo, hi in REGIMES:
                sel_m = (mkt60[ts] > lo) & (mkt60[ts] <= hi) & np.isfinite(mkt60[ts])
                if not sel_m.any():
                    reg[key] = {"label": label, "n_days": 0, "n_samples": 0,
                                "mean_excess": None, "t": None}
                    continue
                rb = _block(daily[sel_m], cnt[sel_m], excess[:, sel_m],
                            hit[:, sel_m], H, ret=ret[:, sel_m],
                            bench=bench[sel_m])
                rb["label"] = label
                reg[key] = rb
            # 反向显著 = 与整体均值**符号相反**且 |t|>2 的市场状态。
            # 符号对称很重要：一条整体为负的规则，如果它在牛市里显著为正，
            # 同样是「只能用在一半的行情里」，必须标出来（老板问的就是这件事）。
            _osign = 1.0 if (blk["mean_excess"] or 0.0) > 0 else -1.0
            rev = [k for k, v in reg.items()
                   if v.get("mean_excess") is not None and v.get("t") is not None
                   and v["mean_excess"] * _osign < 0 and abs(v["t"]) > 2.0]

            results.append({
                "rule": r,
                "category": RULE_CATEGORY[r],
                "category_cn": CATEGORY_CN[RULE_CATEGORY[r]],
                "horizon": H,
                "required": H in CATEGORY_HORIZONS[RULE_CATEGORY[r]],
                "n_days_window": int(ts.size),
                "n_days_low_coverage": int(((sig_dates >= "2023-11-04")
                                            & (sig_dates < "2024-02-07")).sum()),
                **blk,
                "avg_hits_day": round(blk["n_samples"] / max(1, int(ts.size)), 2),
                "pct_limitup_entry": pct_lu,
                "mean_entry_gap": entry_gap,
                "segments": segs,
                "n_seg": n_seg_ok,
                "n_same": n_same,
                "n_opp_sig": n_opp_sig,
                "stable": stable,
                "regimes": reg,
                "regime_reverse": rev,
            })
            if _DUMP_DAILY:
                results[-1]["daily"] = {
                    "dates": [str(x) for x in sig_dates],
                    "excess": [None if not np.isfinite(v) else round(float(v), 4)
                               for v in daily],
                    "bench": [None if not np.isfinite(v) else round(float(v), 4)
                              for v in bench],
                    "count": [int(v) for v in cnt],
                }
        if verbose:
            print("  T+%-3d 完成（累计 %.1fs）" % (H, time.time() - t0), flush=True)
    timings["回测统计"] = round(time.time() - t0, 1)

    # ---------- 6. 覆盖度诊断 ----------
    coverage = []
    for i, d in enumerate(dates):
        if i < sig_start:
            continue
        if d < "2023-11-04":
            continue
        coverage.append({"date": d,
                         "n_stocks": int(present[:, i].sum()),
                         "n_universe": int(uni[:, i].sum())})
    cov_by_month: dict[str, list[int]] = {}
    for row in coverage:
        cov_by_month.setdefault(row["date"][:7], []).append(row["n_stocks"])
    coverage_monthly = [{"month": m,
                         "n_stocks_avg": int(sum(v) / len(v)),
                         "n_stocks_min": int(min(v))}
                        for m, v in sorted(cov_by_month.items())]

    elapsed = round(time.time() - t_start, 1)
    timings["总计"] = elapsed

    return {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "db": str(KBASE.relative_to(ROOT)).replace("\\", "/"),
            "db_adj": "hfq（后复权）",
            "window": {"since": dates[sig_start], "until": until,
                       "signal_days": int(n_sig),
                       "warmup_start": dates[0]},
            "cost_pct": COST_PCT,
            "min_listed_bars": MIN_LISTED_BARS,
            "universe": "全市场（非 ST、上市满 60 个交易日、信号日有行情）",
            "n_stocks": n_stock,
            "n_st_codes": len(st_codes),
            "horizons": need_h,
            "rules": list(rules),
            "category_horizons": {k: list(v) for k, v in CATEGORY_HORIZONS.items()},
            "rule_category": dict(RULE_CATEGORY),
            "segments": [{"start": a, "end": b, "label": lb} for a, b, lb in SEGMENTS],
            "coverage_monthly": coverage_monthly,
            "timings": timings,
            "note": ("t = 按日聚合后的普通 t 检验（每天一个平均超额）；t_nw = Newey-West "
                     "t（滞后阶=H），因为持有 H 天的收益窗口跨日重叠 H−1 天。"),
        },
        "results": results,
    }


_LIMIT_THR_CACHE: dict[str, float] = {}


def _limit_up_thr(code: str) -> float:
    """涨停门槛（近似），口径照 toolbox._limit_up_thr。"""
    v = _LIMIT_THR_CACHE.get(code)
    if v is not None:
        return v
    c = str(code)
    if c[:3] in ("300", "301", "688", "689"):
        v = 19.8
    elif c[:1] in ("4", "8") or c[:3] == "920":
        v = 29.8
    else:
        v = 9.8
    _LIMIT_THR_CACHE[code] = v
    return v


def _date_to_idx(day_list: list[str], cal_map: dict[str, int]) -> np.ndarray:
    """本地 bar 日期 → 日历下标（不在日历里给 -1）。"""
    return np.array([cal_map.get(d, -1) for d in day_list], dtype=np.int64)


_CUSTOM_HORIZONS: tuple[int, ...] = ()
_DUMP_DAILY: bool = False


# ================================================================
# 判定与 CLI
# ================================================================

def verdicts(payload: dict) -> list[dict]:
    """每条规则：挑它分类里「最佳」持有期 → 判定它到底属于哪一类。

    「最佳」= 该分类的三个规定持有期里 t 值最高的那个（样本 ≥ 200 才参评）。
    只在规定的那三个里挑 —— 不许越出分类去挑一个最好看的，那是数据美化。

    判定为什么要分三类（而不是简单的「过 / 不过」）：
      buy      显著为正 + 分段稳定 + 三状态不反向 → 可以直接当买点
      reverse  显著为负 + 分段稳定（负） + 三状态不反向 → 不能当买点，但当**剔除器**
               是有效的：这类信号出现时，之后一段时间大概率跑输大盘
      unstable 分段摇摆或市场状态里出现反向显著 → 没有结论，别用
    把「稳定地错」和「随机」混成一类，是上一版回测最大的信息损失。

    ★ 显著性用双闸：t ≥ 2（按日聚合，硬要求的口径）**且** t_nw ≥ 1。
      t_nw 是 Newey-West（滞后阶 = H）。持有 H 天的收益窗口跨日重叠 H−1 天，
      日聚合只解决同日横截面相关，解决不了这条；H 越大，普通 t 虚高越多。
      只留 t≥2 会把「窗口重叠」当成「规律」，这类假阳性在 H≥60 时尤其多。
    """
    by_rule: dict[str, list[dict]] = {}
    for row in payload["results"]:
        if row.get("required"):
            by_rule.setdefault(row["rule"], []).append(row)

    out = []
    for rule in payload["meta"]["rules"]:
        rows = [r for r in by_rule.get(rule, []) if r["n_samples"] >= 200]
        base = {"rule": rule,
                "category": RULE_CATEGORY[rule],
                "category_cn": CATEGORY_CN[RULE_CATEGORY[rule]]}
        if not rows:
            out.append({**base, "best_horizon": None, "best": None,
                        "pass_horizon": False, "pass_stable": False,
                        "pass_regime": False, "pass_all": False,
                        "verdict_class": "unstable",
                        "reason": "分类规定的持有期里没有一条攒够 200 个样本"})
            continue

        best = max(rows, key=lambda r: (r["t"] if r["t"] is not None else -99))
        t = best["t"] if best["t"] is not None else 0.0
        nw = best["t_nw"] if best["t_nw"] is not None else 0.0
        signif = abs(t) >= 2.0 and abs(nw) >= 1.0
        positive = (best["mean_excess"] or 0.0) > 0

        pass_h = bool(positive and signif)
        pass_s = bool(best["stable"])
        pass_r = not best["regime_reverse"]
        if pass_h and pass_s and pass_r:
            klass, reason = "buy", "三条都过（显著为正 + 分段稳定 + 三状态不反向）"
        elif (not positive) and signif and pass_s and pass_r:
            klass = "reverse"
            reason = ("稳定反向：T+%d 超额 %+.2f%%（t=%.2f / NW %.2f），%d/6 段同向为负、"
                      "反向显著 %d 段，三种市场状态里没有一段显著为正 —— "
                      "不能当买点，但当剔除器是可用的" %
                      (best["horizon"], best["mean_excess"] or 0.0, t, nw,
                       best["n_same"], best["n_opp_sig"]))
        else:
            klass = "unstable"
            bits = []
            if not signif:
                bits.append("T+%d 超额 %+.2f%%，t=%.2f / NW %.2f，显著性不足"
                            % (best["horizon"], best["mean_excess"] or 0.0, t, nw))
            if not pass_s:
                bits.append("分段摇摆（%d/6 同向，反向显著 %d 段）"
                            % (best["n_same"], best["n_opp_sig"]))
            if not pass_r:
                bits.append("市场状态里出现反向显著：%s"
                            % "、".join(best["regime_reverse"]))
            reason = "；".join(bits) or "未达判定门槛"
        out.append({
            **base,
            "best_horizon": best["horizon"],
            "best": {k: best.get(k) for k in
                     ("mean_excess", "t", "t_nw", "win_days", "win_samples",
                      "n_samples", "avg_hits", "avg_hits_day", "mean_ret",
                      "mean_bench", "mean_entry_gap", "pct_limitup_entry",
                      "n_same", "n_opp_sig", "n_seg")},
            "pass_horizon": pass_h, "pass_stable": pass_s, "pass_regime": pass_r,
            "pass_all": klass == "buy",
            "verdict_class": klass,
            "reason": reason,
        })
    return out


def main(argv: list[str] | None = None) -> int:
    global _CUSTOM_HORIZONS, _DUMP_DAILY
    ap = argparse.ArgumentParser(description="开源规则多持有期回测")
    ap.add_argument("--rules", default=",".join(ALL_RULES),
                    help="逗号分隔的规则名，默认全部 11 条")
    ap.add_argument("--horizons", default="",
                    help="强制这批持有期（逗号分隔，如 5,20）；留空则按分类自动取")
    ap.add_argument("--grid", action="store_true", help="跑全持有期网格 1~250")
    ap.add_argument("--since", default="2023-11-04", help="信号窗口起点（默认 3 年）")
    ap.add_argument("--until", default="", help="信号窗口终点（默认库内最后交易日）")
    ap.add_argument("--full-coverage", action="store_true",
                    help="起点改成 2024-02-07（kbase 全市场覆盖开始日，稳健性复核用）")
    ap.add_argument("--out", default="", help="输出 JSON 路径")
    ap.add_argument("--dump-daily", action="store_true",
                    help="把逐日超额序列也写进 JSON（QA 复核 / 画图用）")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    _DUMP_DAILY = bool(a.dump_daily)

    rules = tuple(x.strip() for x in a.rules.split(",") if x.strip())
    bad = [r for r in rules if r not in RULE_CATEGORY]
    if bad:
        sys.exit("未登记的规则：%s（可选：%s）" % (",".join(bad), ",".join(ALL_RULES)))

    since = "2024-02-07" if a.full_coverage else a.since
    until = a.until
    if not until:
        if not KBASE.exists():
            sys.exit("找不到后复权日线库：%s" % KBASE)
        con = sqlite3.connect("file:%s?mode=ro" % KBASE.as_posix(), uri=True)
        until = str(con.execute("SELECT MAX(date) FROM kline").fetchone()[0])
        con.close()

    if a.horizons:
        _CUSTOM_HORIZONS = tuple(int(x) for x in a.horizons.split(",") if x.strip())
        mode = "custom"
    elif a.grid:
        mode = "grid"
    else:
        mode = "category"

    payload = run(since, until, rules, mode, verbose=not a.quiet)
    payload["verdicts"] = verdicts(payload)
    payload["meta"]["mode"] = mode
    payload["meta"]["since_requested"] = since

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if a.out:
        out = Path(a.out)
    else:
        tag = "grid" if mode == "grid" else ("full" if a.full_coverage else "main")
        out = LOG_DIR / ("backtest_oss_%s_%s.json" % (datetime.now().strftime("%Y%m%d"), tag))
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n输出：%s（耗时 %ss）" % (out, payload["meta"]["timings"]["总计"]))
    if not a.quiet:
        print("\n%-22s %-8s %-6s %10s %8s %8s %8s"
              % ("规则", "分类", "持有期", "超额%", "t", "t_nw", "胜率%"))
        for row in payload["results"]:
            if not row["required"] and mode != "custom":
                continue
            print("%-22s %-8s T+%-3d %10.3f %8.2f %8s %8.1f"
                  % (row["rule"], row["category_cn"], row["horizon"],
                     row["mean_excess"] or 0.0, row["t"] or 0.0,
                     ("%.2f" % row["t_nw"]) if row["t_nw"] is not None else "-",
                     row["win_days"] or 0.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
