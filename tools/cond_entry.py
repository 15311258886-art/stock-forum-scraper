# -*- coding: utf-8 -*-
"""A1：结构化买点专项 —— 「触发」到「买点」该怎么走 —— 2026-11-05

## 为什么单独做这一支

`cond_matrix.py` 只测了「等 0/1/3 天买」，那是买点最粗的版本。真实做法是
**触发后等一个条件成立再进**：回踩不破、突破确认、缩量企稳。

## 最关键的设计：配对比较

如果只比「不同买点下的平均收益」，会被**信号集不同**混淆——回踩买点只有部分信号能成交，
而这部分信号本身可能就不一样。

所以这里一律做**配对**：对同一条信号，同时算
  · 该买点下的收益（若成交）
  · 触发日收盘就买的收益（基准 e0）
只保留**该买点能成交**的那些信号，两组相减。这样比的是「同一批信号、不同进场时点」，
信号选择偏差被完全消掉。

## 成交率单独报

买点越苛刻，成交率越低。成交率 12% 的买点即使收益再好，也不是"工具"，是"碰运气"。
所以每个买点都报：成交率、平均等待天数。

## 买点定义（触发日 i 之后 N=10 个交易日内，取首次满足）

  e0            触发日收盘买（基准）
  e1            次日收盘
  e3            第 3 日收盘
  pull_ma5      收盘回到 MA5 之下（回踩到 5 日线）
  pull_2pct     收盘较触发日收盘回撤 ≥2%
  brk_high      收盘站上触发日最高价（突破确认）
  shrink_ma5    量比 ≤0.8 且收盘 ≥MA5（缩量企稳）

## 用法
  python tools/cond_entry.py
  python tools/cond_entry.py --wait 15 --limit 800
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sqlite3
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:      # noqa: BLE001
    pass

from viewer.indicators import compute_indicators  # noqa: E402
from tools.cond_engine import TOOLS, limit_thr  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QFQ = ROOT / "data" / "kline_daily.db"
OUT = ROOT / "viewer" / "logs"

DATA_START = 20100101
COST = 0.0025
MIN_AMOUNT = 5.0e6
MIN_BARS = 260
HOLDS = (3, 5, 10, 20, 60, 120)


def nw_t(x: np.ndarray, lag: int) -> tuple[float, float]:
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return float("nan"), float("nan")
    m = x.mean()
    d = x - m
    var = float((d ** 2).sum()) / n
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        var += 2.0 * w * float((d[k:] * d[:-k]).sum()) / n
    if var <= 0:
        return m, float("nan")
    return m, m / math.sqrt(var / n)


def block_t(x: np.ndarray, stride: int) -> tuple[float, int, float]:
    s = x[::max(1, stride)]
    s = s[np.isfinite(s)]
    n = len(s)
    if n < 4:
        return float("nan"), n, float("nan")
    m = float(s.mean())
    sd = float(s.std(ddof=1))
    if sd <= 0:
        return m, n, float("nan")
    return m, n, m / (sd / math.sqrt(n))


RULES = ("e1", "e3", "pull_ma5", "pull_2pct", "brk_high", "shrink_ma5")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=10, help="触发后最多等几个交易日")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--stamp", default="20261105")
    args = ap.parse_args()
    N = args.wait

    con = sqlite3.connect(str(QFQ))
    dates_all = np.array([int(r[0]) for r in con.execute(
        "SELECT DISTINCT d FROM kline WHERE d>=? ORDER BY d", (DATA_START,))], dtype=np.int64)
    n_dates = len(dates_all)
    dpos = {int(d): i for i, d in enumerate(dates_all)}

    codes = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT code FROM kline WHERE d>=? AND d<=20261104", (DATA_START,))]
    if args.limit:
        codes = codes[:args.limit]
    print("票数 %d；买入规则 %d 种；等待上限 %d 日；持有期 %s"
          % (len(codes), len(RULES), N, HOLDS))

    # acc[(tool, rule, H)] = (每日配对差之和, 每日成交数, 等待天数之和)
    acc = {(k, r, H): (np.zeros(n_dates), np.zeros(n_dates), np.zeros(n_dates))
           for k in TOOLS for r in RULES for H in HOLDS}

    t0 = time.time()
    done = 0
    for code in codes:
        q = con.execute(
            "SELECT d,o,c,h,l,v FROM kline WHERE code=? AND d>=? AND d<=20261104 ORDER BY d",
            (code, DATA_START)).fetchall()
        if len(q) < MIN_BARS + 2:
            continue
        df = pd.DataFrame(q, columns=["d", "open", "close", "high", "low", "volume"])
        df["date"] = pd.to_datetime(df["d"].astype(str), format="%Y%m%d")
        df["code"] = code
        try:
            ind = compute_indicators(df)
        except Exception:      # noqa: BLE001
            continue

        c = df["close"].to_numpy(np.float64)
        h = df["high"].to_numpy(np.float64)
        v = df["volume"].to_numpy(np.float64)
        n = len(c)
        prev = np.concatenate(([np.nan], c[:-1]))
        with np.errstate(divide="ignore", invalid="ignore"):
            ret1 = (c / prev - 1.0) * 100.0
            mom20 = (c / np.concatenate((np.full(20, np.nan), c[:-20])) - 1.0) * 100.0
            high250 = pd.Series(c).rolling(251).max().to_numpy(np.float64)
            dist_high250 = (c / high250 - 1.0) * 100.0
            ma20 = ind["ma20"].to_numpy(np.float64)
            ma20_dev = (c / ma20 - 1.0) * 100.0
            atr_pct = ind["atr"].to_numpy(np.float64) / c * 100.0
        X = dict(close=c, volume=v, ret1=ret1, mom20=mom20,
                 dist_high250=dist_high250, ma20_dev=ma20_dev, atr_pct=atr_pct,
                 ma5=ind["ma5"].to_numpy(np.float64), ma20=ma20,
                 ma60=ind["ma60"].to_numpy(np.float64),
                 ma200=ind["ma200"].to_numpy(np.float64),
                 macd=ind["macd"].to_numpy(np.float64),
                 rsi6=ind["rsi6"].to_numpy(np.float64),
                 wr6=ind["wr6"].to_numpy(np.float64),
                 cci=ind["cci"].to_numpy(np.float64),
                 volume_ratio=ind["volume_ratio"].to_numpy(np.float64))
        X["rand5"] = np.random.default_rng(
            zlib.crc32(code.encode("utf-8")) % (2 ** 32)).random(n)

        amt = c * v
        thr = limit_thr(code)
        base = (np.isfinite(c) & (c > 0) & np.isfinite(v) & (v > 0)
                & (amt >= MIN_AMOUNT) & np.isfinite(X["ma200"]) & np.isfinite(ret1)
                & (ret1 < thr - 0.2))
        if not base.any():
            continue
        gpos_all = np.array([dpos.get(int(d), -1) for d in df["d"].to_numpy()], dtype=np.int64)
        keep = gpos_all >= 0
        base = base & keep
        gpos_all = np.where(keep, gpos_all, 0)

        fwd = {}
        for H in HOLDS:
            f = np.full(n, np.nan)
            if n > H:
                with np.errstate(divide="ignore", invalid="ignore"):
                    f[:n - H] = c[H:] / c[:-H] - 1.0 - COST
            fwd[H] = f

        for k, spec in TOOLS.items():
            mask = spec["fn"](X) & base
            idx = np.flatnonzero(mask)
            if idx.size == 0:
                continue
            S = idx.size
            # 触发日之后 1..N 日的候选位置
            offs = np.arange(1, N + 1)[None, :]
            P = idx[:, None] + offs
            inb = P < n
            Ps = np.clip(P, 0, n - 1)
            c0 = c[idx][:, None]
            h0 = h[idx][:, None]
            with np.errstate(invalid="ignore"):
                cond = {
                    "e1": offs == 1,
                    "e3": offs == 3,
                    "pull_ma5": (c[Ps] <= X["ma5"][Ps]),
                    "pull_2pct": (c[Ps] <= c0 * 0.98),
                    "brk_high": (c[Ps] > h0),
                    "shrink_ma5": ((X["volume_ratio"][Ps] <= 0.8)
                                   & (c[Ps] >= X["ma5"][Ps])),
                }
            for rule in RULES:
                C = np.asarray(cond[rule], dtype=bool) & inb
                any_hit = C.any(axis=1)
                if not any_hit.any():
                    continue
                first = C.argmax(axis=1)          # 0-based 在 offs 里的位置
                wait = (first + 1).astype(np.int64)
                j = idx + wait
                s = np.flatnonzero(any_hit & (j < n))
                if s.size == 0:
                    continue
                jj = j[s]
                ii = idx[s]
                for H in HOLDS:
                    fr = fwd[H][jj]
                    f0 = fwd[H][ii]
                    ok = np.isfinite(fr) & np.isfinite(f0)
                    if not ok.any():
                        continue
                    gp = gpos_all[jj[ok]]
                    d = (fr[ok] - f0[ok])
                    a = acc[(k, rule, H)]
                    a[0][:] += np.bincount(gp, weights=d, minlength=n_dates)
                    a[1][:] += np.bincount(gp, minlength=n_dates)
                    a[2][:] += np.bincount(gp, weights=wait[s][ok].astype(np.float64),
                                           minlength=n_dates)

        done += 1
        if done % 400 == 0:
            print("  已扫 %d 只（%.0fs）" % (done, time.time() - t0))
    con.close()
    print("扫完 %d 只，用时 %.0fs" % (done, time.time() - t0))

    # ---------------- 汇总 ----------------
    rows: list[dict] = []
    for k in TOOLS:
        for rule in RULES:
            for H in HOLDS:
                sm, ct, wt = acc[(k, rule, H)]
                if ct.sum() < 200:
                    continue
                with np.errstate(divide="ignore", invalid="ignore"):
                    daily = np.where(ct > 0, sm / np.maximum(ct, 1), np.nan)
                m1, t1 = nw_t(daily, max(1, H // 5))
                bm, bn, bt = block_t(daily, H)
                # 时间对半分：日均序列前后各一半，同号才算这条买点不是拟合出来的
                dv = daily[np.isfinite(daily)]
                hh = len(dv) // 2
                h1 = float(dv[:hh].mean()) * 100 if hh >= 20 else float("nan")
                h2 = float(dv[hh:].mean()) * 100 if len(dv) - hh >= 20 else float("nan")
                consistent = (np.isfinite(h1) and np.isfinite(h2)
                              and np.sign(h1) == np.sign(h2) == np.sign(m1))
                rows.append({
                    "tool": k, "rule": rule, "hold": H,
                    "signals": int(ct.sum()),
                    "daily_mean_pp": round(m1 * 100, 3),
                    "nw_t": round(t1, 2),
                    "block_n": int(bn),
                    "block_t": None if not np.isfinite(bt) else round(bt, 2),
                    "avg_wait": round(float(wt.sum() / max(ct.sum(), 1)), 1),
                    "half1_pp": None if not np.isfinite(h1) else round(h1, 3),
                    "half2_pp": None if not np.isfinite(h2) else round(h2, 3),
                    "consistent": bool(consistent),
                })

    path = OUT / ("cond_entry_%s.json" % args.stamp)
    path.write_text(json.dumps({"meta": {"wait": N, "holds": list(HOLDS),
                                         "rules": list(RULES), "n_stocks": done,
                                         "note": "配对差 = 该买点收益 − 触发日收盘买收益（同一批信号）",
                                         "cost": COST},
                                "rows": rows}, ensure_ascii=False, indent=1),
                    encoding="utf-8")

    print("\n== 买点 vs 触发日收盘买（配对，同一批信号；正=比立刻买更好）==")
    print("%-20s %-11s %-5s %8s %9s %7s %7s %8s"
          % ("工具", "买点", "持有", "信号数", "配对差pp", "NW t", "非重叠t", "平均等待"))
    for k in TOOLS:
        mine = [r for r in rows if r["tool"] == k]
        if not mine:
            continue
        for r in sorted(mine, key=lambda x: (x["hold"], -x["daily_mean_pp"])):
            print("%-20s %-11s T+%-3d %8d %+9.3f %+7.2f %+7s %8.1f"
                  % (r["tool"], r["rule"], r["hold"], r["signals"], r["daily_mean_pp"],
                     r["nw_t"], "n/a" if r["block_t"] is None else "%+.2f" % r["block_t"],
                     r["avg_wait"]))

    print("\n== 按买点汇总（跨工具平均，看哪种买点普遍更好）==")
    print("%-11s %8s %12s %10s" % ("买点", "单元数", "平均配对差pp", "非重叠t>2占比"))
    for rule in RULES:
        v = [r["daily_mean_pp"] for r in rows if r["rule"] == rule]
        tt = [r for r in rows if r["rule"] == rule and r["block_t"] is not None]
        if not v:
            continue
        share = (sum(1 for r in tt if r["block_t"] > 2.0) / len(tt) * 100) if tt else 0
        print("%-11s %8d %+12.3f %10.0f%%"
              % (rule, len(v), float(np.mean(v)), share))
    print("\n写盘：%s" % path)


if __name__ == "__main__":
    main()
