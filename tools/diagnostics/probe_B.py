# -*- coding: utf-8 -*-
"""方向B 专项探针：底部长期横盘 → 后续爆发，两种买点分开测。

B1 埋伏（左侧）：横盘期间任意一天买入，赌它未来会突破
B2 突破（右侧）：横盘 + d 日放量创 250 日新高时买入

评价三件事，缺一不可：
  最大涨幅（赔率）、期末收益（真实持有）、最大回撤（真实风险）
"""
import sqlite3
import sys
import time
from collections import defaultdict

DB = "data/kline_daily.db"
SINCE = 20140101
HOR = (120, 250, 500)


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


def pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(q * (len(s) - 1))))]


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

    # 分组累加器：key=(组名, 持有期) -> [n, sum_mx, sum_fin, sum_mdd, 各阈值计数]
    acc = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0, 0, 0, 0])
    # [n, sum_max, sum_final, sum_mdd, cnt_max>=100%, cnt_max>=50%, cnt_final>0, cnt_mdd<=-30%]

    total_pts = 0
    for code, bars in iter_bars(conn):
        n = len(bars)
        if n < 560:
            continue
        closes = [b[2] for b in bars]
        vols = [b[5] for b in bars]
        thr = zt_thr(code)
        for i in range(250, n - 120):
            seg = closes[i - 249:i + 1]
            lo, hi = min(seg), max(seg)
            if lo <= 0:
                continue
            amp = (hi - lo) / mean(seg)
            if amp <= 0:
                continue
            pos = (closes[i] - lo) / (hi - lo) if hi > lo else 0.5
            v60 = sum(vols[i - 59:i + 1]) / 60.0
            vr = (vols[i] / v60) if v60 > 0 else 1.0
            amt = vols[i] * closes[i]
            newhigh = closes[i] >= hi * 0.999

            tags = []
            if amp <= 0.45:
                tags.append(("B1a 横盘(振幅≤45%)", "埋伏"))
                if amp <= 0.32:
                    tags.append(("B1b 极度横盘(≤32%)", "埋伏"))
                if amp <= 0.32 and pos < 0.5:
                    tags.append(("B1c 极度横盘+区间下半部", "埋伏"))
            if amp <= 0.45 and newhigh and vr >= 1.5:
                tags.append(("B2a 横盘后放量新高", "突破"))
                if amp <= 0.32:
                    tags.append(("B2b 极度横盘后放量新高", "突破"))
            if amp > 0.9:
                tags.append(("X 对照:高振幅(>90%)", "对照"))
            if not tags:
                continue

            total_pts += 1
            fwd_c = closes[i + 1:i + 501]
            if len(fwd_c) < 120:
                continue
            base = closes[i]
            for hor in HOR:
                fw = fwd_c[:hor]
                if len(fw) < hor:
                    continue
                mx = max(fw) / base - 1.0
                fin = fw[-1] / base - 1.0
                mdd = min(fw) / base - 1.0
                for tag, kind in tags:
                    a = acc[(tag, hor)]
                    a[0] += 1
                    a[1] += mx
                    a[2] += fin
                    a[3] += mdd
                    if mx >= 1.0:
                        a[4] += 1
                    if mx >= 0.5:
                        a[5] += 1
                    if fin > 0:
                        a[6] += 1
                    if mdd <= -0.3:
                        a[7] += 1

    print("候选点 %d，耗时 %.0fs\n" % (total_pts, time.time() - t0))
    for hor in HOR:
        print("=== 持有期 %d 个交易日 ===" % hor)
        print("%-26s %7s %9s %9s %9s %8s %8s %8s %8s" %
              ("组", "n", "最大涨均值", "期末均值", "最大跌均值", "≥100%",
               "≥50%", "期末>0", "跌30%"))
        rows = [(k[0], v) for k, v in acc.items() if k[1] == hor and v[0] >= 200]
        order = {"B2a": 0, "B2b": 1, "B1a": 2, "B1b": 3, "B1c": 4, "X": 5}
        rows.sort(key=lambda r: (order.get(r[0][:3], 9), -r[1][0]))
        for tag, a in rows:
            n_, smx, sfin, smdd, c100, c50, cpos, cmdd = a
            print("%-26s %7d %8.1f%% %8.1f%% %8.1f%% %7.1f%% %7.1f%% %7.1f%% %7.1f%%" %
                  (tag, n_, 100 * smx / n_, 100 * sfin / n_, 100 * smdd / n_,
                   100.0 * c100 / n_, 100.0 * c50 / n_, 100.0 * cpos / n_,
                   100.0 * cmdd / n_))
        print()
    print("耗时 %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    sys.exit(main())
