# -*- coding: utf-8 -*-
"""HOLD 信号「净效应」配对检验（2026-10-30，进哥要求：只剩这一个能用，确认它真有用吗？）

已有结论（probe_crowd_robust.py）：HOLD T+5 超额 −3.38%，t=−3.16，三项稳健性全过。
但那只是「HOLD 的票后来跌了」。跌可能是因为**这些票在发帖前已经涨了一波**（实证：前 5 日超额 +6.37%），
那就是纯粹的动量反转 —— 我们的量价因子本来就能算，不需要任何论坛数据。

所以真正要回答的是：**扣掉「前期涨幅」之后，HOLD 还剩多少信息？**

方法（事件研究的标准配对法）：
  对每个 HOLD 事件 (code, d)：
    取同日、前期涨幅（pre5 超额）与该事件相差 ≤2pp 的**全市场其他股票**作对照组
    净效应 = HOLD 的 fwd5 超额 − 对照组 fwd5 超额均值
  t 值仍按「日聚合后再检验」（与其它探针同口径）。

同时给三个部署可行性数字：
  · 每个交易日平均几个 HOLD 标的（决定它能不能当日常清单用）
  · HOLD 命中的票里，有多少已经被 avoid.py 亮灯（决定它有没有增量）
  · pre5 分桶后的净效应（决定要不要只取「涨多的」那一档）

用法：python tools/probe_hold_net.py
"""
from __future__ import annotations

import sqlite3
import statistics
from bisect import bisect_left
from collections import defaultdict

DB = "viewer/app.db"
H = 5                 # 前向/回看窗口（交易日）
MATCH_PP = 2.0        # 配对容差：前期涨幅差 ≤2 个百分点
START = "2026-08-01"


def tstat(vals):
    n = len(vals)
    if n < 5:
        return None
    m = statistics.fmean(vals)
    sd = statistics.stdev(vals)
    return m / (sd / n ** 0.5) if sd else None


def fmt_t(t):
    return "%.2f" % t if t is not None else "-"


def main():
    con = sqlite3.connect(DB)

    cal = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]
    print("交易日历 %s .. %s（%d 日）" % (cal[0], cal[-1], len(cal)))

    # ---- 1. 载入 pre5 / fwd5 ----
    print("[1/5] 载入 pre5/fwd5 …", flush=True)
    sql = """
    SELECT code, date, close,
           LAG(close, %d)  OVER (PARTITION BY code ORDER BY date),
           LEAD(close, %d) OVER (PARTITION BY code ORDER BY date)
      FROM stock_indicators_daily
     WHERE date >= '2026-06-01'
    """ % (H, H)
    pre5 = defaultdict(dict)     # code -> date -> pct
    fwd5 = defaultdict(dict)
    day_pre = defaultdict(list)
    day_fwd = defaultdict(list)
    for code, date, close, c5, f5 in con.execute(sql):
        if not close or close <= 0:
            continue
        if c5 and c5 > 0:
            v = (close / c5 - 1.0) * 100.0
            pre5[code][date] = v
            day_pre[date].append(v)
        if f5 and f5 > 0:
            v = (f5 / close - 1.0) * 100.0
            fwd5[code][date] = v
            day_fwd[date].append(v)
    pre_mean = {d: statistics.fmean(v) for d, v in day_pre.items() if len(v) >= 100}
    fwd_mean = {d: statistics.fmean(v) for d, v in day_fwd.items() if len(v) >= 100}

    # 同日全市场按 pre5 超额排序，供配对查表
    by_day = defaultdict(list)   # date -> [(pre_ex, fwd_ex, code)]
    for code, dm in fwd5.items():
        for d, v in dm.items():
            p = pre5.get(code, {}).get(d)
            if p is None or d not in pre_mean or d not in fwd_mean:
                continue
            by_day[d].append((p - pre_mean[d], v - fwd_mean[d], code))
    for d in by_day:
        by_day[d].sort()

    # ---- 2. 抽 HOLD 事件 ----
    print("[2/5] 抽 HOLD 事件 …", flush=True)
    seen = set()
    ev = []
    for author, code, pt in con.execute(
            "SELECT author, symbol, post_time FROM trades "
            "WHERE action='HOLD' AND post_time<>'' AND symbol IS NOT NULL AND symbol<>''"):
        d = pt[:10]
        i = bisect_left(cal, d)
        if i >= len(cal):
            continue
        ad = cal[i]
        if ad < START:
            continue
        k = (author, code, ad)
        if k in seen:
            continue
        seen.add(k)
        ev.append((author, code, ad))
    print("     HOLD 事件 %d 条（去重后），覆盖交易日 %d 个"
          % (len(ev), len({e[2] for e in ev})))

    # ---- 3. 覆盖率 / 每日标的数 ----
    print("\n" + "=" * 90)
    print("① 部署可行性：它到底能给出多少个标的？")
    print("=" * 90)
    per_day = defaultdict(set)
    for _a, code, ad in ev:
        per_day[ad].add(code)
    cnts = sorted(len(v) for v in per_day.values())
    n_all = len(set(c for _a, c, _d in ev))
    print("  涉及不同标的 %d 只 / 事件 %d 条 / 交易日 %d 个" % (n_all, len(ev), len(per_day)))
    print("  每个交易日标的数：中位 %d，均值 %.1f，最多 %d，最少 %d"
          % (statistics.median(cnts), statistics.fmean(cnts), cnts[-1], cnts[0]))
    print("  → 全市场 5300 只里，每天最多筛出 %d 只（占 %.1f%%）" % (cnts[-1], 100.0 * cnts[-1] / 5300))

    # ---- 4. 原始 vs 配对净效应 ----
    print("\n" + "=" * 90)
    print("② 核心检验：扣掉「前期涨幅」之后，HOLD 还剩多少信息？")
    print("=" * 90)
    raw_d, net_d = defaultdict(list), defaultdict(list)
    raw_all, net_all = [], []
    matched_ok = 0
    for _a, code, ad in ev:
        fd = fwd5.get(code, {}).get(ad)
        if fd is None or ad not in fwd_mean:
            continue
        ex = fd - fwd_mean[ad]
        p = pre5.get(code, {}).get(ad)
        if p is None or ad not in pre_mean:
            continue
        pex = p - pre_mean[ad]
        # 同日、前期涨幅相近的对照组
        pool = by_day.get(ad) or []
        ctrl = [f for pp, f, cc in pool if abs(pp - pex) <= MATCH_PP and cc != code]
        raw_all.append(ex)
        raw_d[ad].append(ex)
        if len(ctrl) >= 10:
            net = ex - statistics.fmean(ctrl)
            net_all.append(net)
            net_d[ad].append(net)
            matched_ok += 1
    t_raw = tstat([statistics.fmean(v) for v in raw_d.values()])
    t_net = tstat([statistics.fmean(v) for v in net_d.values()])
    print("  原始（复现旧结论）     n=%-4d 超额 %+.3f%%  t=%s" % (len(raw_all), statistics.fmean(raw_all), fmt_t(t_raw)))
    print("  配对净效应（同伴对比） n=%-4d 净差 %+.3f%%  t=%s   ← 关键"
          % (len(net_all), statistics.fmean(net_all), fmt_t(t_net)))
    if matched_ok < len(raw_all):
        print("  （%d 条事件因同日无足够对照被剔除）" % (len(raw_all) - matched_ok))
    lost = abs(statistics.fmean(raw_all)) - abs(statistics.fmean(net_all))
    print("\n  解释：HOLD 的 %.2f%% 里，有 %.2f%% 可由「同类前期涨幅的普通股票」解释；"
          % (abs(statistics.fmean(raw_all)), lost))
    print("        剩下的 %.2f%%（t=%s）才是 HOLD 独有的信息。"
          % (abs(statistics.fmean(net_all)), fmt_t(t_net)))
    print("  ★判定：%s" % ("有独立信息，值得留" if abs(t_net or 0) >= 2.0
                        else "扣掉前期涨幅后不显著 → 只是动量代理，可用量价因子替代"))

    # ---- 5. pre5 分桶 ----
    print("\n" + "=" * 90)
    print("③ 分档：是不是只有「涨多的」那档才跌？（决定能不能收窄条件）")
    print("=" * 90)
    buckets = (("≤0（没涨）", -999, 0), ("0~5%", 0, 5), ("5~10%", 5, 10), ("≥10%（追高）", 10, 999))
    for name, lo, hi in buckets:
        sub_raw = defaultdict(list)
        sub_net = defaultdict(list)
        n = 0
        for _a, code, ad in ev:
            fd = fwd5.get(code, {}).get(ad)
            p = pre5.get(code, {}).get(ad)
            if fd is None or p is None or ad not in fwd_mean or ad not in pre_mean:
                continue
            pex = p - pre_mean[ad]
            if not (lo < pex <= hi):
                continue
            ex = fd - fwd_mean[ad]
            pool = by_day.get(ad) or []
            ctrl = [f for pp, f, cc in pool if abs(pp - pex) <= MATCH_PP and cc != code]
            sub_raw[ad].append(ex)
            if len(ctrl) >= 10:
                sub_net[ad].append(ex - statistics.fmean(ctrl))
            n += 1
        if n == 0:
            continue
        rv = [statistics.fmean(v) for v in sub_raw.values()]
        nv = [statistics.fmean(v) for v in sub_net.values()]
        print("  %-14s n=%-4d 原始 %+.3f%% (t=%s)   净效应 %+.3f%% (t=%s)"
              % (name, n, statistics.fmean([x for v in sub_raw.values() for x in v]), fmt_t(tstat(rv)),
                 statistics.fmean([x for v in sub_net.values() for x in v]) if sub_net else 0,
                 fmt_t(tstat(nv))))

    # ---- 6. 与 avoid 的重叠 ----
    print("\n" + "=" * 90)
    print("④ 增量：HOLD 命中的票，我们的 avoid.py 是不是已经亮灯了？")
    print("=" * 90)
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer"))
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import avoid
        lvl_of = lambda s: (s.get("level") if isinstance(s, dict) else None)
        # 逐日回算：把当日 HOLD 票丢进 avoid 打一次分（avoid 用「现在」的 K 线，故只能用最新日）；
        # 历史日不可复算 → 这里只对最近 5 个交易日的 HOLD 票做重叠检查，并明确标注局限
        recent = sorted(c for _a, c, d in ev if d >= cal[-6])
        recent = sorted(set(recent))
        if not recent:
            print("  最近 5 个交易日无 HOLD 事件")
        else:
            res = avoid.compute(con, recent)
            hit = [(c, res[c]["score"], res[c]["level"]) for c in recent if res.get(c)]
            bright = [x for x in hit if x[2] in ("high", "mid")]
            print("  最近 5 个交易日 HOLD 标的 %d 只，avoid 打分可得 %d 只" % (len(recent), len(hit)))
            print("  其中 avoid 已亮灯（high/mid）%d 只，占 %.0f%%"
                  % (len(bright), 100.0 * len(bright) / max(1, len(hit))))
            for c, s, l in hit[:12]:
                print("    %s  风险分 %-2s  %s" % (c, s, l or "-"))
    except Exception as e:  # noqa: BLE001
        print("  （avoid 重叠检查跳过：%s）" % e)

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
