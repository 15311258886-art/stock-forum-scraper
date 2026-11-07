# -*- coding: utf-8 -*-
"""方向A 专项探针：短线 T+1 超跌修复，全量复核。

口径（刻意最严）：
- d 日盘后出信号（用当日收盘才知道的信息）
- d+1 开盘买入；若 d+1 开盘即涨停则买不进 → 剔除
- d+1+h 开盘卖出（h=1 即「今天买明天卖」，T+1 制度下限）
- 超额 = 个股收益 − 同日全市场等权收益（已剔次新股/异常值）
- t 值：先按日聚合样本，再对日度超额序列做 t 检验
- 上市不满 120 个交易日不计样本与基准
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


CASES = [
    # name, 单日跌幅门槛, 累计跌幅日数, 破 MA20 深度, 量比上限
    ("跌5%+破MA20",        -0.05, 0, 0.03, None),
    ("跌7%+破MA20",        -0.07, 0, 0.03, None),
    ("跌5%+破MA20+缩量0.8", -0.05, 0, 0.03, 0.8),
    ("跌5%+破MA20+放量1.5", -0.05, 0, 0.03, -1),
    ("三日累跌12%",         None,  3, 0.03, None),
    ("破MA20达12%",        None,  0, 0.12, None),
    ("跌9%+破MA20深度8%",   -0.09, 0, 0.08, None),
]


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
                    cell = acc[bars[i][0]]
                    cell[0] += 1
                    cell[1] += x
    bench = {d: s / k for d, (k, s) in acc.items() if k >= 300}
    print("基准日 %d 个，全期均值 %+.4f%%/日，%.0fs"
          % (len(bench), 100 * mean(list(bench.values())), time.time() - t0))
    panic = {d for d, m in bench.items() if m <= -0.015}
    print("恐慌日 %d 个" % len(panic))
    del acc

    stat = {}
    for cn, *_ in CASES:
        for h in HS:
            for g in ("ALL", "恐慌", "平稳"):
                stat[(cn, h, g)] = [0, 0.0, defaultdict(lambda: [0, 0.0])]

    for code, bars in iter_bars(conn):
        n = len(bars)
        if n < 130:
            continue
        closes = [b[2] for b in bars]
        vols = [b[5] for b in bars]
        thr = zt_thr(code)
        ma20 = sum(closes[:20])
        for i in range(n):
            if i >= 20:
                ma20 += closes[i] - closes[i - 20]
            if i < 120 or i > n - 2:
                continue
            d0 = bars[i][0]
            bm = bench.get(d0)
            if bm is None:
                continue
            o1 = bars[i + 1][1]
            if o1 <= 0 or o1 >= closes[i] * (1 + thr) * 0.998:
                continue
            m20 = ma20 / 20.0
            if m20 <= 0:
                continue
            r1 = closes[i] / closes[i - 1] - 1.0 if closes[i - 1] > 0 else 0.0
            v20 = sum(vols[i - 19:i + 1]) / 20.0
            vr = (vols[i] / v20) if v20 > 0 else 1.0
            dev = closes[i] / m20 - 1.0
            g = "恐慌" if d0 in panic else "平稳"
            for cn, ret_th, dd, dev_th, vr_th in CASES:
                if dd:
                    base = closes[i - dd]
                    if base <= 0 or closes[i] / base - 1.0 > -0.12:
                        continue
                elif ret_th is not None and r1 > ret_th:
                    continue
                if dev > -dev_th:
                    continue
                if vr_th is not None:
                    if vr_th > 0 and vr < vr_th:
                        continue
                    if vr_th < 0 and vr < -vr_th:
                        continue
                for h in HS:
                    j = i + 1 + h
                    if j >= n:
                        continue
                    o2 = bars[j][1]
                    if o2 <= 0:
                        continue
                    ret = o2 / o1 - 1.0
                    for gg in (g, "ALL"):
                        st = stat[(cn, h, gg)]
                        st[0] += 1
                        st[1] += ret
                        cell = st[2][d0]
                        cell[0] += 1
                        cell[1] += ret - bm

    print("\n=== 方向A 短线（d+1 开盘买 → d+1+h 开盘卖；超额对同日全市场）===")
    print("%-22s %1s %4s %7s %6s %8s %9s %7s %7s" %
          ("条件", "h", "组", "n", "日数", "绝对%", "超额%", "t", "胜率%"))
    for (cn, h, g), (n, sraw, daily) in stat.items():
        if n < 300:
            continue
        dser = [(v[1] / v[0], v[0]) for v in daily.values() if v[0] >= 3]
        if len(dser) < 30:
            continue
        xs = [x[0] for x in dser]
        t = tstat(xs)
        print("%-22s %1d %4s %7d %6d %8.3f %9.3f %7.2f %7.1f" %
              (cn, h, g, n, len(xs), 100 * sraw / n, 100 * mean(xs), t or 0,
               100.0 * sum(1 for x in xs if x > 0) / len(xs)))
    print("\n耗时 %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    sys.exit(main())
