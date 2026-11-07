# -*- coding: utf-8 -*-
"""校验：本机重算的指标 vs 产品 `stock_indicators_daily` —— 2026-11-05

## 为什么必须校这一下

条件化回测的每一个判据（rsi6<30、量比≥1.5、距 52 周高 ≤25%…）都长在指标上。
指标如果和产品不是同一套，那测出来的就不是产品在用的那个工具，整份结论作废。
产品自己的指标表 `stock_indicators_daily` 有 261 天（2025-09-30 ~ 2026-11-03）——
正好拿来当基准答案。

## 怎么校

抽若干只票，把它们在指标表里的每一天，与我用 `viewer.indicators.compute_indicators`
在全历史上重算的值逐列比。看中位绝对偏差与最大偏差。

预期：ma/macd/rsi 这类应几乎完全一致；差异主要来自产品计算时的**起始窗口**（预热长度）
不同 —— 我的窗口更长，理论上更准。

## 用法
  python tools/audit_indicators.py
"""
from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from viewer.indicators import compute_indicators  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QFQ = ROOT / "data" / "kline_daily.db"
APP = ROOT / "viewer" / "app.db"

COLS = ["ma5", "ma20", "ma60", "ma200", "macd", "rsi6", "wr6", "cci", "atr", "volume_ratio"]
SAMPLE = 40


def main() -> None:
    con_app = sqlite3.connect(str(APP))
    con_q = sqlite3.connect(str(QFQ))

    codes = [r[0] for r in con_app.execute(
        "SELECT code FROM stock_indicators_daily GROUP BY code"
        " HAVING COUNT(*)>=200 ORDER BY code")]
    print("指标表里满 200 天的票：%d 只" % len(codes))
    step = max(1, len(codes) // SAMPLE)
    pick = codes[::step][:SAMPLE]
    print("抽样 %d 只" % len(pick))

    stats = {c: [] for c in COLS}
    for code in pick:
        rows = con_q.execute(
            "SELECT d,o,c,h,l,v FROM kline WHERE code=? ORDER BY d", (code,)).fetchall()
        if len(rows) < 300:
            continue
        df = pd.DataFrame(rows, columns=["d", "open", "close", "high", "low", "volume"])
        df["date"] = pd.to_datetime(df["d"].astype(str), format="%Y%m%d")
        df["code"] = code
        ind = compute_indicators(df)
        ind["d"] = df["d"].to_numpy()
        got = {str(int(r["d"])): r for r in ind.to_dict("records")}

        ref = con_app.execute(
            "SELECT date,code," + ",".join(COLS) + " FROM stock_indicators_daily WHERE code=?",
            (code,)).fetchall()
        for rr in ref:
            key = str(rr[0]).replace("-", "")
            g = got.get(key)
            if not g:
                continue
            for i, cname in enumerate(COLS):
                a = rr[2 + i]
                b = g.get(cname)
                if a is None or b is None or not np.isfinite(float(b)):
                    continue
                stats[cname].append(abs(float(a) - float(b)))

    print("\n%-14s %8s %10s %10s %10s" % ("指标", "样本", "中位绝对差", "p95", "最大"))
    for cname in COLS:
        v = np.array(stats[cname], dtype=np.float64)
        if len(v) == 0:
            print("%-14s 无样本" % cname)
            continue
        print("%-14s %8d %10.5f %10.5f %10.5f"
              % (cname, len(v), np.median(v), np.percentile(v, 95), v.max()))
    con_app.close()
    con_q.close()


if __name__ == "__main__":
    main()
