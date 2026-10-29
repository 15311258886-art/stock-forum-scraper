# -*- coding: utf-8 -*-
"""方向A 补测：A 股短线的正超额到底在哪一类形态里。

已验证「超跌买入」是系统性负超额。那反过来问：
涨停首板 / 放量突破 / 创新高 / 强势整理 —— 这些「动量类」形态有没有正超额？

口径与 probe_A 完全一致（d+1 开盘买，d+1+h 开盘卖，剔次新股与开盘涨停）。
"""
import sqlite3
import sys
import time
from collections import defaultdict

DB = "data/kline_daily.db"
SINCE = 20160101
HS = (1, 3, 5)


def zt_thr(code):
    if code[:2] in ("30", "68"):
        return 0.198
    if code[:3] in ("43", "83", "87", "88", "92"):
        return 0.295
    return 0.098


def iter_bars(conn):
    cur = conn.execute(
        "SELECT code,d,o,c,h,l,v FROM kline WHERE d>=? AND o>0 AND c>0 ORDER BY code,d",
        (SINCE,))
    cc, buf = None, []
    for code, d, o, cl, h, lo, v in cur:
        if code != cc:
            if buf:
                yield cc, buf
            cc, buf = code, []
        buf.append((d, o, cl, h, lo, v or 0.0))
    if buf:
        yield cc, buf


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def tstat(xs):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    sd = (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5
    return (m / (sd / n ** 0.5)) if sd > 0 else None


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA cache_size=-200000")

    acc = defaultdict(lambda: [0, 0.0])
    for code, bars in iter_bars(conn):
        n = len(bars)
        for i in range(120, n - 2):
            o1, o2 = bars[i + 1][1], bars[i + 2][1]
            if o1 > 0 and o2 > 0:
                x = o2 / o1 - 1.0
                if -0.35 < x < 0.35:
                    c = acc[bars[i][0]]
                    c[0] += 1
                    c[1] += x
    bench = {d: s / k for d, (k, s) in acc.items() if k >= 300}
    print("基准日 %d，全期均值 %+.4f%%/日，%.0fs"
          % (len(bench), 100 * mean(list(bench.values())), time.time() - t0))
    del acc

    names = ["涨停(全部)", "首板(前一日未涨停)", "二连板", "三连板及以上",
             "放量突破(涨4%+量比2)", "创60日新高+放量",
             "强势整理(3日缩量不破MA5)"]
    stat = {}
    for nm in names:
        for h in HS:
            stat[(nm, h)] = [0, 0.0, defaultdict(lambda: [0, 0.0])]
    stat[("ALL 全市场", 1)] = [0, 0.0, defaultdict(lambda: [0, 0.0])]

    for code, bars in iter_bars(conn):
        n = len(bars)
        if n < 130:
            continue
        closes = [b[2] for b in bars]
        vols = [b[5] for b in bars]
        thr = zt_thr(code)
        ma5 = sum(closes[:5])
        for i in range(n):
            if i >= 5:
                ma5 += closes[i] - closes[i - 5]
            if i < 120 or i > n - 2:
                continue
            d0 = bars[i][0]
            bm = bench.get(d0)
            if bm is None:
                continue
            o1 = bars[i + 1][1]
            if o1 <= 0:
                continue
            if o1 >= closes[i] * (1 + thr) * 0.998:
                continue
            r1 = closes[i] / closes[i - 1] - 1.0 if closes[i - 1] > 0 else 0.0
            r2 = closes[i - 1] / closes[i - 2] - 1.0 if i >= 2 and closes[i - 2] > 0 else 0.0
            v20 = sum(vols[i - 19:i + 1]) / 20.0
            vr = (vols[i] / v20) if v20 > 0 else 1.0
            hi60 = max(closes[i - 59:i + 1])
            tags = []
            if r1 >= thr * 0.98:
                tags.append("涨停(全部)")
                if r2 < thr * 0.98:
                    tags.append("首板(前一日未涨停)")
                elif i >= 3 and closes[i - 2] / closes[i - 3] - 1.0 < thr * 0.98:
                    tags.append("二连板")
                else:
                    tags.append("三连板及以上")
            if r1 >= 0.04 and vr >= 2.0:
                tags.append("放量突破(涨4%+量比2)")
            if closes[i] >= hi60 * 0.999 and vr >= 1.5:
                tags.append("创60日新高+放量")
            if ma5 > 0 and closes[i] > ma5 / 5.0:
                last3 = [closes[i - 2] / closes[i - 3] - 1, closes[i - 1] / closes[i - 2] - 1,
                         closes[i] / closes[i - 1] - 1]
                v3 = sum(vols[i - 2:i + 1]) / 3.0
                if sum(1 for x in last3 if x > 0) >= 2 and v3 < v20 * 0.8:
                    tags.append("强势整理(3日缩量不破MA5)")
            if not tags:
                # 全市场基准样本（只取 h=1 一次，避免重复计数）
                j = i + 2
                if j < n and bars[j][1] > 0:
                    st = stat[("ALL 全市场", 1)]
                    st[0] += 1
                    st[1] += bars[j][1] / o1 - 1.0
                    c = st[2][d0]
                    c[0] += 1
                    c[1] += bars[j][1] / o1 - 1.0 - bm
                continue
            for tag in tags:
                for h in HS:
                    j = i + 1 + h
                    if j >= n or bars[j][1] <= 0:
                        continue
                    ret = bars[j][1] / o1 - 1.0
                    st = stat[(tag, h)]
                    st[0] += 1
                    st[1] += ret
                    c = st[2][d0]
                    c[0] += 1
                    c[1] += ret - bm

    print("\n=== 动量类形态（d+1 开盘买 → d+1+h 开盘卖；超额对同日全市场）===")
    print("%-24s %1s %7s %6s %8s %9s %7s %7s" %
          ("条件", "h", "n", "日数", "绝对%", "超额%", "t", "胜率%"))
    for (nm, h), (n_, sraw, daily) in stat.items():
        if n_ < 300:
            continue
        dser = [v[1] / v[0] for v in daily.values() if v[0] >= 3]
        if len(dser) < 30:
            continue
        t = tstat(dser)
        print("%-24s %1d %7d %6d %8.3f %9.3f %7.2f %7.1f" %
              (nm, h, n_, len(dser), 100 * sraw / n_, 100 * mean(dser),
               t or 0, 100.0 * sum(1 for x in dser if x > 0) / len(dser)))
    print("\n耗时 %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    sys.exit(main())
