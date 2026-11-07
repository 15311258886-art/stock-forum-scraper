# -*- coding: utf-8 -*-
"""方向B 复核：横盘组 vs 全市场基准，并做分年度稳定性检验。

只有同时满足三条才算真结论：
  1. 跑赢「什么都不选」的全市场基准
  2. 每个年份都成立（不是被某一轮牛市拉起来的）
  3. 回撤可控（胜率不能靠少数暴涨股撑起来）
"""
import sqlite3
import sys
import time
from collections import defaultdict

DB = "data/kline_daily.db"
SINCE = 20140101
HOR = (250, 500)


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

    # key=(组, 持有期, 年份) -> [n, sum_fin, sum_mdd, cnt_pos, cnt_mdd30]
    acc = defaultdict(lambda: [0, 0.0, 0.0, 0, 0])

    for code, bars in iter_bars(conn):
        n = len(bars)
        if n < 560:
            continue
        closes = [b[2] for b in bars]
        for i in range(250, n - 120):
            base = closes[i]
            if base <= 0:
                continue
            yr = bars[i][0] // 10000
            seg = closes[i - 249:i + 1]
            lo, hi = min(seg), max(seg)
            if lo <= 0:
                continue
            amp = (hi - lo) / mean(seg)
            groups = []
            if i % 20 == 0:
                groups.append("ALL 全市场")
            if amp <= 0.45:
                groups.append("横盘≤45%")
                if amp <= 0.32:
                    groups.append("极度横盘≤32%")
            if not groups:
                continue
            for hor in HOR:
                fw = closes[i + 1:i + 1 + hor]
                if len(fw) < hor:
                    continue
                fin = fw[-1] / base - 1.0
                mdd = min(fw) / base - 1.0
                for g in groups:
                    for key in ((g, hor, "全期"), (g, hor, yr)):
                        a = acc[key]
                        a[0] += 1
                        a[1] += fin
                        a[2] += mdd
                        if fin > 0:
                            a[3] += 1
                        if mdd <= -0.3:
                            a[4] += 1

    for hor in HOR:
        print("=" * 78)
        print("持有期 %d 个交易日" % hor)
        print("%-20s %8s %9s %9s %9s %9s" %
              ("组 / 年份", "n", "期末均值", "胜率", "最大跌均值", "跌30%占比"))
        allrow = acc.get(("ALL 全市场", hor, "全期"))
        for g in ("ALL 全市场", "横盘≤45%", "极度横盘≤32%"):
            a = acc.get((g, hor, "全期"))
            if not a or a[0] < 100:
                continue
            n_, sfin, smdd, cpos, cm30 = a
            print("%-20s %8d %8.1f%% %8.1f%% %8.1f%% %8.1f%%" %
                  (g, n_, 100 * sfin / n_, 100.0 * cpos / n_,
                   100 * smdd / n_, 100.0 * cm30 / n_))
        if allrow:
            bn = allrow[0]
            bfin = allrow[1] / bn
            print("  --- 相对全市场基准（超额）---")
            for g in ("横盘≤45%", "极度横盘≤32%"):
                a = acc.get((g, hor, "全期"))
                if a and a[0] >= 100:
                    print("  %-18s 超额 %+7.2f%%  胜率差 %+6.2f%%" %
                          (g, 100 * (a[1] / a[0] - bfin),
                           100.0 * (a[3] / a[0] - allrow[3] / bn)))
            print("  --- 分年度（横盘≤45% vs 全市场）---")
            print("  %-6s %8s %9s %9s %9s" % ("年份", "n", "横盘期末", "全市场", "超额"))
            for yr in range(2014, 2026):
                ga = acc.get(("横盘≤45%", hor, yr))
                ba = acc.get(("ALL 全市场", hor, yr))
                if not ga or not ba or ga[0] < 300 or ba[0] < 100:
                    continue
                gf = ga[1] / ga[0]
                bf = ba[1] / ba[0]
                print("  %-6d %8d %8.1f%% %8.1f%% %+8.1f%%" %
                      (yr, ga[0], 100 * gf, 100 * bf, 100 * (gf - bf)))
        print()
    print("耗时 %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    sys.exit(main())
