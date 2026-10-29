# -*- coding: utf-8 -*-
"""方向B 关键复核：区分「一直死气沉沉」与「曾经活跃后沉寂」。

绝对振幅筛选会选出银行/公用事业（天然低波、永不太涨）。
用户要的是「底部蓄势待爆发」，对应的形态应是：
    前期振幅很大（曾经是活跃股）→ 近期振幅压缩（被市场遗忘）→ 之后重新爆发

三种假设一次测完，并与全市场对照。
"""
import sqlite3
import sys
import time
from collections import defaultdict

DB = "data/kline_daily.db"
SINCE = 20130101
HOR = 500


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


def amp_of(seg):
    lo, hi = min(seg), max(seg)
    if lo <= 0:
        return None
    m = mean(seg)
    return (hi - lo) / m if m > 0 else None


def main():
    t0 = time.time()
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA cache_size=-200000")

    # [n, sum_fin, cnt_pos, sum_mdd, cnt_big(>=100%), sum_amt]
    acc = defaultdict(lambda: [0, 0.0, 0, 0.0, 0, 0.0])

    for code, bars in iter_bars(conn):
        n = len(bars)
        if n < 760:
            continue
        cl = [b[2] for b in bars]
        vl = [b[5] for b in bars]
        for i in range(500, n - HOR):
            b0 = cl[i]
            if b0 <= 0:
                continue
            now_seg = cl[i - 249:i + 1]          # 近 250 日
            prev_seg = cl[i - 499:i - 249]       # 前 250 日
            a_now = amp_of(now_seg)
            a_prev = amp_of(prev_seg)
            if a_now is None or a_prev is None or a_prev <= 0:
                continue
            rel = a_now / a_prev
            amt = vl[i] * b0
            fw = cl[i + 1:i + 1 + HOR]
            if len(fw) < HOR:
                continue
            fin = fw[-1] / b0 - 1.0
            mdd = min(fw) / b0 - 1.0
            groups = []
            if i % 20 == 0:
                groups.append("ALL 全市场")
            # 假设1：绝对横盘（当前做法）
            if a_now <= 0.45:
                groups.append("绝对横盘≤45%")
            # 假设2：曾经活跃后沉寂
            if a_prev >= 0.90 and a_now <= 0.45:
                groups.append("曾活跃(>90%)现横盘(≤45%)")
            if a_prev >= 0.90 and a_now <= 0.32:
                groups.append("曾活跃(>90%)现极度横盘(≤32%)")
            # 假设3：相对压缩
            if rel <= 0.60 and a_now <= 0.60:
                groups.append("相对压缩比≤0.60")
            # 对照：一直死气沉沉（前后都低）
            if a_prev <= 0.45 and a_now <= 0.45:
                groups.append("一直低波(前后都≤45%)")
            for g in groups:
                a = acc[g]
                a[0] += 1
                a[1] += fin
                a[2] += 1 if fin > 0 else 0
                a[3] += mdd
                a[4] += 1 if fw[-1] / b0 - 1.0 >= 1.0 or max(fw) / b0 - 1.0 >= 1.0 else 0
                a[5] += amt

    print("=== 持有 %d 个交易日（约2年），2013-2026 ===\n" % HOR)
    print("%-30s %9s %9s %8s %9s %9s %9s" %
          ("组", "n", "期末均值", "胜率", "最大跌均值", "曾涨≥100%", "中位成交额亿"))
    order = ["ALL 全市场", "绝对横盘≤45%", "一直低波(前后都≤45%)", "相对压缩比≤0.60",
             "曾活跃(>90%)现横盘(≤45%)", "曾活跃(>90%)现极度横盘(≤32%)"]
    base = acc.get("ALL 全市场")
    for g in order:
        a = acc.get(g)
        if not a or a[0] < 200:
            continue
        n_, sfin, cpos, smdd, cbig, samt = a
        print("%-30s %9d %8.1f%% %7.1f%% %8.1f%% %8.1f%% %9.2f" %
              (g, n_, 100 * sfin / n_, 100.0 * cpos / n_, 100 * smdd / n_,
               100.0 * cbig / n_, samt / n_ / 1e8))
    if base:
        print("\n相对全市场超额（期末均值差）：")
        for g in order[1:]:
            a = acc.get(g)
            if a and a[0] >= 200:
                print("  %-30s %+6.2f%%   胜率差 %+5.2f%%" %
                      (g, 100 * (a[1] / a[0] - base[1] / base[0]),
                       100.0 * (a[2] / a[0] - base[2] / base[0])))
    print("\n耗时 %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    sys.exit(main())
