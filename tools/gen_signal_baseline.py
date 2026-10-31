# -*- coding: utf-8 -*-
"""补算信号回测基线（2026-10-29）

## 为什么单独一个脚本

`signal_backtest.backtest()` 里基线是顺带算出来的，但它要先对 135 万行逐行跑
`_derive_signals`（推导 17 个指标），全量约 18 分钟。而基线只是 close 的前向收益分布，
与信号无关 —— 用 numpy 批量算一分钟内就能完成。

## 口径必须与 signal_backtest 严格对齐

两处约束（否则会算出一个偏乐观或偏悲观的基线，所有"超额胜率"跟着失真）：

1. 同样限制在最近 `lookback` 个交易日内（回测里是 `start = max(0, n - lookback_days)`）。
2. 前向收益用 `close[i+fwd] / close[i] - 1`，且 i+fwd 必须在样本内（尾部不足 fwd 的
   交易日直接丢弃，不能用未来数据补齐）。

## 用法

    .venv/Scripts/python.exe -m tools.gen_signal_baseline
    .venv/Scripts/python.exe -m tools.gen_signal_baseline --lookback 250 --min-bars 60
"""
import argparse
import math
import os
import sqlite3
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _sharpe(a) -> float:
    if len(a) < 2:
        return 0.0
    sd = float(a.std(ddof=1))
    if sd == 0 or math.isnan(sd):
        return 0.0
    return float(a.mean() / sd * math.sqrt(244.0))


def gen(lookback: int = 250, forward=(5, 10, 20), min_bars: int = 60) -> dict:
    from viewer.signal_backtest import ensure_tables

    db = os.path.join(ROOT, "viewer", "app.db")
    con = sqlite3.connect(db, timeout=30)
    ensure_tables(con)
    t0 = time.time()

    codes = [r[0] for r in con.execute("SELECT DISTINCT code FROM stock_indicators_daily")]
    print("[baseline] 标的 %d 只 | 回看 %d 日 | 前向 %s 日" % (len(codes), lookback, forward), flush=True)

    buckets = {f: [] for f in forward}
    done = 0
    for code in codes:
        rows = con.execute(
            "SELECT close FROM stock_indicators_daily WHERE code=? ORDER BY date", (code,)).fetchall()
        if len(rows) < min_bars:
            continue
        closes = np.array([r[0] for r in rows], dtype=float)
        n = len(closes)
        start = max(0, n - lookback)
        for fwd in forward:
            # 取 [start, n) 区间内的每条记录，配对 i+fwd；尾部不足的直接丢
            i0 = start
            i1 = n - fwd
            if i1 <= i0:
                continue
            c0 = closes[i0:i1]
            c1 = closes[i0 + fwd:i1 + fwd]
            mask = np.isfinite(c0) & np.isfinite(c1) & (c0 != 0)
            if not mask.any():
                continue
            buckets[fwd].append((c1[mask] / c0[mask]) - 1.0)
        done += 1
        if done % 1000 == 0:
            print("[baseline] %d/%d" % (done, len(codes)), flush=True)

    out, recs = {}, []
    for fwd in forward:
        if not buckets[fwd]:
            continue
        a = np.concatenate(buckets[fwd])
        a = a[np.isfinite(a)]
        if not len(a):
            continue
        wr = float(np.mean(a > 0))
        avg = float(a.mean())
        recs.append((fwd, int(len(a)), round(wr, 4), round(avg, 4), ""))
        out[fwd] = {"n": int(len(a)), "winrate": round(wr, 4), "avg_ret": round(avg, 4),
                    "sharpe": round(_sharpe(a), 3)}
        print("[baseline] 前向%2d日: n=%-8d 胜率=%.2f%% 均值=%+.3f%% 夏普=%.3f"
              % (fwd, len(a), wr * 100, avg * 100, _sharpe(a)), flush=True)

    if recs:
        con.executemany(
            "INSERT OR REPLACE INTO signal_baseline(fwd,n,winrate,avg_ret,updated) VALUES (?,?,?,?,?)",
            recs)
        con.commit()
    con.close()
    print("[baseline] 完成，用时 %.1fs" % (time.time() - t0), flush=True)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=250)
    ap.add_argument("--min-bars", type=int, default=60)
    a = ap.parse_args()
    gen(lookback=a.lookback, min_bars=a.min_bars)
