#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""解禁「时点」探针（2026-10-30）。

第一个探针（probe_lift_signal.py）发现：解禁前 20 日超额 -1.39%(t=-4.06)，
解禁后 T+5 反而 +0.46%(t=+2.04)。但这事有可能是假象，必须排除三种解释：

  解释A「这些票本来就在阴跌」 → 安慰剂检验：随机抽同样数量的(code,date)，
      若随机样本的 [-20,0] 也跌 1.4%，说明与解禁无关。
  解释B「只是均值回归/短期超跌反弹」 → 看剂量反应：占流通比例越高，
      解禁前应该跌得越多。没有剂量反应 = 不是解禁造成的。
  解释C「只有某一段行情如此」 → 时间对半切，两段必须同号。

本脚本输出**日度衰减曲线**（相对解禁日的第 t 个交易日，平均日超额是多少），
这是回答「提前多久提示才不是噪音」的唯一依据——
如果抛压只在解禁前 10 天才出现，提前 3 个月提示就是错的。

用法：
  python tools/probe_lift_timing.py
  python tools/probe_lift_timing.py --start 2025-08-18
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import statistics
import sys
from bisect import bisect_left
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")

W_LEFT = 60      # 解禁前看 60 个交易日
W_RIGHT = 20     # 解禁后看 20 个交易日


def tstat(vals):
    n = len(vals)
    if n < 5:
        return None
    m = statistics.fmean(vals)
    s = statistics.stdev(vals)
    return None if s == 0 else m / (s / n ** 0.5)


def build_daily_excess(con):
    """每个 (code, date) 的「日收益 − 同日全市场均值」。并按 code 存日期序列。"""
    rows = con.execute(
        "SELECT code, date, close FROM stock_indicators_daily"
        " WHERE close>0 ORDER BY code, date").fetchall()
    seq = defaultdict(list)
    for code, d, c in rows:
        seq[code].append((d, c))
    day_r = defaultdict(list)
    ex = {}
    for code, arr in seq.items():
        for i in range(1, len(arr)):
            d, c = arr[i]
            pc = arr[i - 1][1]
            if pc <= 0:
                continue
            r = (c / pc - 1.0) * 100.0
            day_r[d].append(r)
            ex[(code, d)] = r
    m = {k: statistics.fmean(v) for k, v in day_r.items()}
    for k in ex:
        ex[k] -= m.get(k[1], 0.0)
    # code -> 按日期索引的 list，便于按偏移取
    idx = {c: [d for d, _ in arr] for c, arr in seq.items()}
    return ex, idx, m


def curve(events, ex, idx, lo, hi):
    """返回 {offset: [日超额...]}，offset 相对解禁日（0 = 解禁当日）。"""
    out = defaultdict(list)
    for code, d in events:
        ds = idx.get(code)
        if not ds:
            continue
        i = bisect_left(ds, d)
        if i >= len(ds) or ds[i] != d:
            # 解禁日不在该票 K 线里（停牌/未上市），跳过
            if i >= len(ds):
                continue
        for off in range(lo, hi + 1):
            j = i + off
            if j < 0 or j >= len(ds):
                continue
            v = ex.get((code, ds[j]))
            if v is not None:
                out[off].append(v)
    return out


def cum_at(cv, lo, upto):
    """累积到 upto 的日超额之和（用于画累积曲线）。"""
    s = 0.0
    for off in range(lo, upto + 1):
        v = cv.get(off)
        if v:
            s += statistics.fmean(v)
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-08-18")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    print("[1/4] 构建日超额 …", flush=True)
    ex, idx, mkt = build_daily_excess(con)
    print("      覆盖 %d 个(code,date) 日收益，%d 个交易日" % (len(ex), len(mkt)))

    # ---- 事件：解禁日对齐到该票的第一个 >= 解禁日的交易日 ----
    raw = con.execute(
        "SELECT code, name, free_date, type_group, free_ratio, total_ratio,"
        " lift_cap, n_holder FROM lift_event WHERE free_date>=?", (args.start,)).fetchall()
    # 同一票同一天多批合并，避免重复计权
    merged = {}
    for r in raw:
        ds = idx.get(r["code"])
        if not ds:
            continue
        i = bisect_left(ds, r["free_date"])
        if i >= len(ds):
            continue
        key = (r["code"], ds[i])
        if key in merged:
            merged[key]["fr"] = (merged[key]["fr"] or 0) + (r["free_ratio"] or 0)
            merged[key]["cap"] = (merged[key]["cap"] or 0) + (r["lift_cap"] or 0)
        else:
            merged[key] = dict(tg=r["type_group"], fr=r["free_ratio"] or 0,
                               tr=r["total_ratio"] or 0, cap=r["lift_cap"] or 0,
                               nh=r["n_holder"], name=r["name"])
    events = list(merged.keys())
    # 只保留有足够左侧历史的（否则前 60 日不全）
    ok = []
    for code, d in events:
        ds = idx[code]
        i = ds.index(d) if d in ds else -1
        if i < 0 or i < 25:          # 至少要有 25 天历史，能算前20日
            continue
        ok.append((code, d))
    events = ok
    print("      解禁事件 %d 个（有足够历史）" % len(events))

    print("\n[2/4] 安慰剂：随机抽同样数量的(code,date) 作对照 …")
    rnd = random.Random(20261030)
    pool = [(c, d) for (c, d) in ex if c in idx]
    placebo = [rnd.choice(pool) for _ in range(len(events))]

    print("\n[3/4] 计算偏移曲线 …")
    cv = curve(events, ex, idx, -W_LEFT, W_RIGHT)
    pv = curve(placebo, ex, idx, -W_LEFT, W_RIGHT)

    # ---- 分段（把 -60..-1 切成 4 段，看哪段开始转负）----
    segs = [(-60, -41), (-40, -21), (-20, -11), (-10, -1), (0, 0), (1, 5), (6, 20)]

    def seg_stat(cv_, lo, hi):
        vals = []
        for off in range(lo, hi + 1):
            vals.extend(cv_.get(off) or [])
        return vals, len(vals)

    print("\n" + "=" * 78)
    print("【解禁事件 vs 安慰剂 · 分段平均日超额】")
    print("%-16s %10s %10s %10s %8s" % ("窗口(相对解禁日)", "事件组", "安慰剂", "差值", "事件t"))
    print("-" * 78)
    for lo, hi in segs:
        vals, n = seg_stat(cv, lo, hi)
        pvals, pn = seg_stat(pv, lo, hi)
        if n < 20:
            continue
        # 事件组 t：按偏移日聚合成序列（同一天的解禁事件仍会重叠，故用「按偏移均值序列」）
        per_off = [statistics.fmean(v) for o, v in cv.items()
                   if lo <= o <= hi and len(v) >= 3]
        t = tstat(per_off)
        pmean = statistics.fmean(pvals) if pvals else 0.0
        emean = statistics.fmean(vals)
        print("%-16s %+9.3f%% %+9.3f%% %+9.3f%% %8s"
              % ("[%d, %d]" % (lo, hi), emean, pmean, emean - pmean,
                 ("%.2f" % t) if t is not None else "-"))
    print("\n注：t 用「按偏移日的均值序列」算（%d 个点），比按样本算保守；"
          % sum(1 for o, v in cv.items() if o >= -W_LEFT and len(v) >= 3))
    print("    安慰剂组若不显著异于 0，而事件组显著，则说明下跌与解禁有关。")

    # ---- 累积曲线（关键：从哪天开始转负）----
    print("\n" + "=" * 78)
    print("【累积超额曲线】从解禁前 60 日起累加，看何时转负")
    print("%-10s %12s %12s" % ("相对日", "事件累积", "安慰剂累积"))
    print("-" * 78)
    for off in (-60, -50, -40, -30, -25, -20, -15, -10, -5, -1, 0, 5, 10, 20):
        ce = cum_at(cv, -W_LEFT, off)
        cp = cum_at(pv, -W_LEFT, off)
        print("%-10d %+11.2f%% %+11.2f%%" % (off, ce, cp))

    # ---- 剂量反应：占流通比例越高，解禁前跌得越多？----
    print("\n" + "=" * 78)
    print("【剂量反应检验】解禁前 20 日累积超额 vs 占流通股比例")
    print("  若真由解禁抛压造成，比例越高应跌越多；若无单调关系，则不是解禁的锅。")
    print("%-14s %7s %12s %9s" % ("占流通比例", "n", "前20日累积超额", "t"))
    print("-" * 78)
    buckets = [(0, 0.01, "<1%"), (0.01, 0.03, "1~3%"), (0.03, 0.10, "3~10%"),
               (0.10, 0.30, "10~30%"), (0.30, 99, ">30%")]
    for lo_, hi_, name in buckets:
        sub = [(c, d) for (c, d) in events if lo_ <= (merged[(c, d)]["fr"] or 0) < hi_]
        if len(sub) < 15:
            print("%-14s %7d  (样本不足)" % (name, len(sub)))
            continue
        scv = curve(sub, ex, idx, -20, -1)
        # 每个事件自己的累积（不是按偏移均值），更贴近真实持有体验
        per_event = []
        for code, d in sub:
            ds = idx[code]
            i = bisect_left(ds, d)
            s = 0.0
            got = 0
            for j in range(i - 20, i):
                if j < 0:
                    continue
                v = ex.get((code, ds[j]))
                if v is not None:
                    s += v
                    got += 1
            if got >= 15:
                per_event.append(s)
        if len(per_event) < 15:
            print("%-14s %7d  (有效样本不足)" % (name, len(per_event)))
            continue
        t = tstat(per_event)
        print("%-14s %7d %+11.2f%% %9s"
              % (name, len(per_event), statistics.fmean(per_event),
                 ("%.2f" % t) if t is not None else "-"))

    # ---- 时间对半切 ----
    print("\n" + "=" * 78)
    print("【时间对半切 · 解禁前 20 日累积超额】两段必须同号")
    dates = sorted({d for _, d in events})
    mid = dates[len(dates) // 2] if dates else None
    for half, tag in ((lambda d: d < mid, "前半段"), (lambda d: d >= mid, "后半段")):
        sub = [(c, d) for (c, d) in events if half(d)]
        per_event = []
        for code, d in sub:
            ds = idx[code]
            i = bisect_left(ds, d)
            s, got = 0.0, 0
            for j in range(i - 20, i):
                if j < 0:
                    continue
                v = ex.get((code, ds[j]))
                if v is not None:
                    s += v
                    got += 1
            if got >= 15:
                per_event.append(s)
        if len(per_event) < 15:
            continue
        t = tstat(per_event)
        print("  %-8s n=%-4d 累积 %+7.2f%%  t=%s"
              % (tag, len(per_event), statistics.fmean(per_event),
                 ("%.2f" % t) if t is not None else "-"))
    print("  切点 = %s" % mid)

    print("\n" + "=" * 78)
    print("【按解禁类型 · 解禁前 20 日累积超额】")
    for tg in ("定增解禁", "首发原股东", "股权激励", "其他"):
        sub = [(c, d) for (c, d) in events if merged[(c, d)]["tg"] == tg]
        per_event = []
        for code, d in sub:
            ds = idx[code]
            i = bisect_left(ds, d)
            s, got = 0.0, 0
            for j in range(i - 20, i):
                if j < 0:
                    continue
                v = ex.get((code, ds[j]))
                if v is not None:
                    s += v
                    got += 1
            if got >= 15:
                per_event.append(s)
        if len(per_event) < 15:
            print("  %-10s n=%-4d (不足)" % (tg, len(per_event)))
            continue
        t = tstat(per_event)
        print("  %-10s n=%-4d 累积 %+7.2f%%  t=%s"
              % (tg, len(per_event), statistics.fmean(per_event),
                 ("%.2f" % t) if t is not None else "-"))

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
