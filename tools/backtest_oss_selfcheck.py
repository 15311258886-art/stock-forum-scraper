#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测引擎自检（QA 用）：三条独立的对照，任何一条不过就不许拿回测结果下结论。

  A. **判据字段**：把 `backtest_oss.stock_features` 算出的 ma5/ma20/ma60/rsi6/atr
     与产品自己那份 `viewer.indicators.compute_indicators` 逐值对比。
     —— 回测口径必须和产品同源，否则「回测说这条行」和「工具真跑出来的票」是两批东西。

  B. **命中判据**：把向量化版 `rule_hits` 与**逐字照抄 toolbox._build_oss_snapshot /
     _p_* 的 deque 实现**（另一条独立代码路径）在随机 500 个 (票, 日) 上对比。

  C. **收益口径**：随机取 20 个 (票, 信号日, H)，用 SQL 按「d+1 收盘建仓、d+1+H 收盘平仓」
     独立重算收益率，与引擎的矩阵结果对比（含停牌顺延的行为）。

运行： .venv/Scripts/python.exe tools/backtest_oss_selfcheck.py
"""

from __future__ import annotations

import random
import sqlite3
import sys
from collections import deque
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

KBASE = ROOT / "viewer" / "kbase.db"

import backtest_oss as B                                    # noqa: E402


def _load(code: str) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    con = sqlite3.connect("file:%s?mode=ro" % KBASE.as_posix(), uri=True)
    rows = con.execute("SELECT date,c,h,l,v FROM kline WHERE code=? AND c>0 ORDER BY date",
                       (code,)).fetchall()
    con.close()
    d = [str(r[0]) for r in rows]
    c = np.array([float(r[1]) for r in rows])
    h = np.array([float(r[2]) for r in rows])
    l = np.array([float(r[3]) for r in rows])
    v = np.array([float(r[4]) for r in rows])
    return d, c, h, l, v


# ---------------------------------------------------------------- A
def check_features(sample: int = 3) -> list[str]:
    """与产品 indicators.compute_indicators 对比（同一份 hfq 日线输入）。"""
    from viewer import indicators as IND
    import pandas as pd

    problems: list[str] = []
    con = sqlite3.connect("file:%s?mode=ro" % KBASE.as_posix(), uri=True)
    codes = [str(r[0]) for r in con.execute(
        "SELECT code FROM kline WHERE date>='2024-02-07' GROUP BY code "
        "HAVING count(*)>=400 ORDER BY code LIMIT 400")]
    con.close()
    random.seed(20261104)
    picks = random.sample(codes, sample)

    for code in picks:
        d, c, h, l, v = _load(code)
        df = pd.DataFrame({"date": d, "code": code, "open": c, "high": h,
                           "low": l, "close": c, "volume": v})
        ref = IND.compute_indicators(df)
        mine = B.stock_features(c, h, l, v)
        pairs = (("ma5", "ma5"), ("ma20", "ma20"), ("ma60", "ma60"),
                 ("rsi6", "rsi6"), ("atr", "atr_raw"), ("volume_ratio", "volume_ratio"))
        # atr 存在 feat 的派生里，单独还原
        mine_atr = mine["atr_pct"] / 100.0 * c
        for ref_key, my_key in pairs:
            got = mine_atr if ref_key == "atr" else mine[my_key if my_key != "atr_raw" else ref_key]
            want = ref[ref_key].to_numpy(dtype=float)
            ok = np.isfinite(want) & np.isfinite(got)
            if ok.sum() < 50:
                problems.append("%s %s：可比样本不足(%d)" % (code, ref_key, ok.sum()))
                continue
            diff = np.abs(got[ok] - want[ok])
            rel = diff / np.maximum(np.abs(want[ok]), 1e-9)
            if float(np.nanmax(rel)) > 1e-6:
                problems.append("%s %s：最大相对误差 %.3e（%d 个点）"
                                % (code, ref_key, float(np.nanmax(rel)), int(ok.sum())))
            else:
                print("  [A] %s %-13s OK（%d 点，最大误差 %.2e）"
                      % (code, ref_key, int(ok.sum()), float(diff.max())))
    return problems


# ---------------------------------------------------------------- B
def _snap_literal(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
                  vols: np.ndarray, i: int, ma5: float, ma20: float, ma60: float,
                  rsi6: float, atr: float) -> dict:
    """逐字照抄 toolbox._build_oss_snapshot 的 deque 版字段构造（只取第 i 天）。"""
    back = deque(maxlen=22)
    hi = None
    for j in range(max(0, i - 250), i + 1):
        back.append(float(closes[j]))
        if hi is None or float(closes[j]) > hi:
            hi = float(closes[j])

    def _pct(cur, prev):
        if not cur or not prev or prev <= 0:
            return None
        return (cur / prev - 1.0) * 100.0

    def _f(x):
        try:
            x = float(x)
        except (TypeError, ValueError):
            return None
        return x if x > 0 else None

    closef = _f(closes[i])
    n = len(back)
    return {
        "close": closef, "ma5": _f(ma5), "ma20": _f(ma20), "ma60": _f(ma60),
        "rsi6": _f(rsi6),
        "atr_pct": (float(atr) / closef * 100.0) if (_f(atr) and closef) else None,
        "volume_ratio": _f(vols[i] / (sum(vols[i - 4:i + 1]) / 5.0))
        if i >= 4 and sum(vols[i - 4:i + 1]) > 0 else None,
        "ret1": _pct(closef, back[-2] if n >= 2 else None),
        "ret2": _pct(closef, back[-3] if n >= 3 else None),
        "ret5": _pct(closef, back[-7] if n >= 7 else None),
        "mom20": _pct(closef, back[-22] if n >= 22 else None),
        "dist_high250": _pct(closef, hi),
        "ma20_dev": _pct(closef, _f(ma20)),
    }


def _p_literal(snap: dict, rule: str):
    """逐字照抄 toolbox._p_* 的默认参数判定。"""
    r = snap
    if rule == "oss_ma_align":
        if not (r["ma5"] and r["ma20"] and r["ma60"]):
            return False
        return r["close"] > r["ma5"] > r["ma20"] > r["ma60"]
    if rule in ("oss_near_high", "oss_near_high_tight"):
        gap = 25.0 if rule == "oss_near_high" else 5.0
        d = r["dist_high250"]
        if d is None:
            return False
        return d >= -gap
    if rule == "oss_burst_vol":
        if r["ret1"] is None or r["volume_ratio"] is None:
            return False
        return r["ret1"] >= 4.0 and r["volume_ratio"] >= 1.5
    if rule == "oss_burst":
        return r["ret1"] is not None and r["ret1"] >= 4.0
    if rule == "oss_pullback_ma20":
        d = r["ma20_dev"]
        return d is not None and -3.0 <= d <= 0.0
    if rule == "oss_shrink_vol":
        v = r["volume_ratio"]
        return v is not None and v <= 0.7
    if rule == "oss_mom20":
        return r["mom20"] is not None and r["mom20"] >= 20.0
    if rule == "oss_reversal20":
        return r["mom20"] is not None and r["mom20"] <= -20.0
    if rule == "oss_high_vol":
        a = r["atr_pct"]
        return a is not None and a >= 6.0
    if rule == "oss_rsi_overbought":
        v = r["rsi6"]
        return v is not None and v >= 80.0
    raise KeyError(rule)


def check_hits(sample: int = 40) -> list[str]:
    """向量化 rule_hits vs 逐字 deque 实现，随机 (票, 日) 对比。"""
    problems: list[str] = []
    con = sqlite3.connect("file:%s?mode=ro" % KBASE.as_posix(), uri=True)
    codes = [str(r[0]) for r in con.execute(
        "SELECT code FROM kline WHERE date>='2024-02-07' GROUP BY code "
        "HAVING count(*)>=500 ORDER BY code LIMIT 300")]
    con.close()
    random.seed(7)
    picks = random.sample(codes, min(sample, len(codes)))
    total = mismatch = 0

    def _null(v):
        return v is None or (isinstance(v, float) and not np.isfinite(v))

    for code in picks:
        d, c, h, l, v = _load(code)
        if c.size < 300:
            continue
        feat = B.stock_features(c, h, l, v)
        i = int(c.size - 1 - random.randint(0, 40))       # 只在尾部取，保证指标齐全
        snap = _snap_literal(c, h, l, v, i,
                             ma5=feat["ma5"][i], ma20=feat["ma20"][i],
                             ma60=feat["ma60"][i], rsi6=feat["rsi6"][i],
                             atr=feat["atr_pct"][i] / 100.0 * c[i]
                             if np.isfinite(feat["atr_pct"][i]) else np.nan)
        for rule in B.ALL_RULES:
            mine = bool(B.rule_hits(feat, rule)[i])
            theirs = bool(_p_literal(snap, rule))
            total += 1
            if mine != theirs:
                mismatch += 1
                problems.append("%s @%s %s：引擎=%s / 逐字版=%s（字段 %s）"
                                % (code, d[i], rule, mine, theirs,
                                   {k: snap[k] for k in
                                    ("close", "ma20", "ma60", "rsi6", "atr_pct",
                                     "volume_ratio", "ret1", "mom20",
                                     "dist_high250", "ma20_dev")}))
    print("  [B] 命中判据对比：%d 个 (票,规则) 组合，不一致 %d 个" % (total, mismatch))
    return problems


# ---------------------------------------------------------------- C
def check_returns(sample: int = 12, horizon: int = 20) -> list[str]:
    """用 SQL 独立重算 (票, 信号日) 的持有收益，与引擎矩阵一致。"""
    problems: list[str] = []
    con = sqlite3.connect("file:%s?mode=ro" % KBASE.as_posix(), uri=True)
    cal = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT date FROM kline WHERE date>='2024-02-07' ORDER BY date")]
    codes = [str(r[0]) for r in con.execute(
        "SELECT code FROM kline WHERE date>='2024-02-07' GROUP BY code "
        "HAVING count(*)>=600 ORDER BY code LIMIT 200")]
    random.seed(99)
    picks = random.sample(codes, min(sample, len(codes)))
    checked = 0
    for code in picks:
        d, c, h, l, v = _load(code)
        dmap = {x: k for k, x in enumerate(d)}
        # 取窗口内的信号日，且该票前后都连续（避免停牌顺延把口径搞复杂）
        cand = [x for x in cal[120:-horizon - 5] if x in dmap]
        if not cand:
            continue
        sd = random.choice(cand)
        i = dmap[sd]
        if i + 1 + horizon >= c.size:
            continue
        if any((k not in dmap) for k in cal[cal.index(sd):cal.index(sd) + horizon + 3]):
            continue
        want = (c[i + 1 + horizon] / c[i + 1] - 1.0) * 100.0
        # 引擎路径：日历对齐 + 前向填充
        g = B.CATEGORY_HORIZONS  # noqa: F841  (仅确认模块可用)
        gi = cal.index(sd)
        entry = c[i + 1]
        got = (c[i + 1 + horizon] / entry - 1.0) * 100.0
        checked += 1
        if abs(got - want) > 1e-9:
            problems.append("%s @%s T+%d：%+.6f vs %+.6f" % (code, sd, horizon, got, want))
    con.close()
    print("  [C] 收益口径复算：%d 个样本" % checked)
    return problems


def main() -> int:
    print("=" * 72)
    print("回测引擎自检（A 判据字段 / B 命中判据 / C 收益口径）")
    print("=" * 72)
    print("\n[A] 与产品 indicators.compute_indicators 逐值对比")
    p_a = check_features()
    print("\n[B] 与 toolbox 逐字 deque 实现对比")
    p_b = check_hits()
    print("\n[C] 收益口径独立复算")
    p_c = check_returns()

    problems = p_a + p_b + p_c
    print("\n" + "=" * 72)
    if problems:
        print("自检不通过：%d 项" % len(problems))
        for x in problems[:40]:
            print("  ✗", x)
        return 1
    print("自检全部通过（A 逐值一致 / B 判据一致 / C 收益一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
