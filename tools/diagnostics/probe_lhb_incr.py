# -*- coding: utf-8 -*-
"""龙虎榜：增量信息检验 + 衰减曲线 + 与 avoid 现有规则重叠（2026-10-30 夜）

为什么还要做这一步（上一轮 HOLD 的教训）：
  HOLD 的 -3.08% 里有 1.1% 是「跟它涨得差不多的股票也会跌」，扣掉后不显著。
  龙虎榜上榜票的共同特征是「当日放量 + 大涨/大跌」，这些 avoid.py 里已有规则能捕捉
  （放量下跌 t=-5.91、当日暴跌 t=-4.20）。所以必须先回答：

  ★ 在「同样放量、同样涨幅」的普通股票里，龙虎榜上榜票是不是额外更差？

方法（同档配对）：
  对每个龙虎榜事件 (code, d)：
    取同日、涨幅方向相同、量比在同一档（>5 / 3~5 / 2~3 / 1~2）的**未上榜**股票作对照
    净效应 = 事件超额 − 对照组超额均值
  若净效应仍显著负 → 龙虎榜带来独立信息，值得接入
  若归零 → 它只是「放量大涨」的影子，用现成量价因子即可，不必依赖榜单

另输出：
  · 衰减：T+1 / T+3 / T+5 / T+10 超额，判断「躲几天」
  · 与 avoid 现有 6 条规则的重叠率，判断真实增量标的数

用法：python tools/probe_lhb_incr.py
"""
from __future__ import annotations

import statistics
import sqlite3
from collections import defaultdict
import os

DB = os.environ.get("VIEWER_DB") or "viewer/app.db"
HS = (1, 3, 5, 10)
MIN_DAY_N = 100


def tstat(vals):
    n = len(vals)
    if n < 5:
        return None
    m = statistics.fmean(vals)
    sd = statistics.stdev(vals)
    return m / (sd / n ** 0.5) if sd else None


def ft(t):
    return "%.2f" % t if t is not None else "-"


def vratio_bucket(v):
    if v is None:
        return None
    if v >= 5:
        return "量比≥5"
    if v >= 3:
        return "量比3-5"
    if v >= 2:
        return "量比2-3"
    if v >= 1:
        return "量比1-2"
    return "量比<1"


def main():
    con = sqlite3.connect(DB)
    cal = [r[0] for r in con.execute("SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]
    n = len(cal)
    print("[载入] %s .. %s（%d 日）" % (cal[0], cal[-1], n), flush=True)

    # 一次性载入 close / volume_ratio
    px = defaultdict(dict)
    vr = defaultdict(dict)
    for code, d, c, v in con.execute(
            "SELECT code, date, close, volume_ratio FROM stock_indicators_daily WHERE close>0"):
        px[code][d] = c
        vr[code][d] = v

    # 预计算：每日每票的 ret1（当日涨幅）、多周期前向收益
    ret1 = defaultdict(dict)          # code -> date -> 当日涨幅%
    fwd = {H: {} for H in HS}         # H -> (code,date) -> 可执行 H 日收益
    day_mean = {H: defaultdict(list) for H in HS}
    maxh = max(HS)
    for code, dm in px.items():
        for i in range(1, n):
            d = cal[i]
            c0 = dm.get(cal[i - 1])
            c1 = dm.get(d)
            if c0 and c1:
                ret1[code][d] = (c1 / c0 - 1.0) * 100.0
            for H in HS:
                j = i + 1 + H
                if j >= n:
                    continue
                cb = dm.get(cal[i + 1]) if i + 1 < n else None
                ce = dm.get(cal[j])
                if cb and ce:
                    fwd[H][(code, d)] = (ce / cb - 1.0) * 100.0
                    day_mean[H][d].append((ce / cb - 1.0) * 100.0)
    dm_mean = {H: {d: statistics.fmean(v) for d, v in day_mean[H].items() if len(v) >= MIN_DAY_N}
               for H in HS}

    # 每日对照池：date -> (方向, 量比档) -> [超额...]（用 T+5）
    pool = defaultdict(lambda: defaultdict(list))
    for code, dm in px.items():
        for d in dm:
            H = 5
            f = fwd[H].get((code, d))
            if f is None or d not in dm_mean[H]:
                continue
            r = ret1.get(code, {}).get(d)
            if r is None:
                continue
            b = vratio_bucket(vr.get(code, {}).get(d))
            if b is None:
                continue
            dirn = "涨" if r > 0 else ("跌" if r < 0 else "平")
            pool[d][(dirn, b)].append(f - dm_mean[H][d])

    # 龙虎榜事件
    evs = {}
    for d, code, tr in con.execute("SELECT trade_date, security_code, turnoverrate FROM lhb_stocks"):
        try:
            trv = float(tr or 0)
        except Exception:
            trv = 0.0
        if (code, d) not in evs or trv > evs[(code, d)]:
            evs[(code, d)] = trv
    print("[龙虎榜] 去重事件 %d 个" % len(evs))

    # ---------- ① 衰减 ----------
    print("\n" + "=" * 96)
    print("① 衰减曲线：上榜后各周期超额（次日可执行口径，t 按日聚合）")
    print("=" * 96)
    print("%-12s %-30s %5s %9s %8s %8s" % ("分组", "", "n", "超额%", "t", "胜率"))
    print("-" * 96)
    sub = {"换手≥30%（最该躲）": [(c, d) for (c, d), tr in evs.items() if tr >= 30],
           "换手20-30%": [(c, d) for (c, d), tr in evs.items() if 20 <= tr < 30],
           "全部上榜": list(evs.keys())}
    for label, lst in sub.items():
        for H in HS:
            per_day = defaultdict(list)
            allv = []
            for c, d in lst:
                f = fwd[H].get((c, d))
                if f is None or d not in dm_mean[H]:
                    continue
                ex = f - dm_mean[H][d]
                per_day[d].append(ex)
                allv.append(ex)
            if len(allv) < 10:
                continue
            dv = [statistics.fmean(v) for v in per_day.values() if v]
            print("%-12s T+%-28d %5d %+8.2f%% %8s %7.1f%%"
                  % (label, H, len(allv), statistics.fmean(allv), ft(tstat(dv)),
                     100.0 * sum(1 for v in allv if v > 0) / len(allv)))
        print()

    # ---------- ② 同档配对净效应 ----------
    print("=" * 96)
    print("② 关键检验：扣掉「同样放量、同样涨跌」的普通股票之后，上榜票还剩多少？")
    print("=" * 96)
    raw_d, net_d = defaultdict(list), defaultdict(list)
    raw_all, net_all = [], []
    unmatched = 0
    for (c, d), tr in evs.items():
        H = 5
        f = fwd[H].get((c, d))
        if f is None or d not in dm_mean[H]:
            continue
        r = ret1.get(c, {}).get(d)
        b = vratio_bucket(vr.get(c, {}).get(d))
        if r is None or b is None:
            unmatched += 1
            continue
        dirn = "涨" if r > 0 else ("跌" if r < 0 else "平")
        ctrl = pool[d].get((dirn, b)) or []
        ex = f - dm_mean[H][d]
        raw_all.append(ex)
        raw_d[d].append(ex)
        if len(ctrl) >= 20:
            net = ex - statistics.fmean(ctrl)
            net_all.append(net)
            net_d[d].append(net)
        else:
            unmatched += 1
    t_raw = tstat([statistics.fmean(v) for v in raw_d.values() if v])
    t_net = tstat([statistics.fmean(v) for v in net_d.values() if v])
    print("  原始超额外                    n=%-4d %+.2f%%   t=%s" % (len(raw_all), statistics.fmean(raw_all), ft(t_raw)))
    print("  同量比同方向配对净效应         n=%-4d %+.2f%%   t=%s    ← 关键"
          % (len(net_all), statistics.fmean(net_all) if net_all else 0, ft(t_net)))
    print("  （%d 个事件因同日同档对照不足 20 只被剔除）" % unmatched)
    if net_all:
        keep = abs(statistics.fmean(net_all)) / max(1e-9, abs(statistics.fmean(raw_all)))
        print("\n  净效应保留原幅度的 %.0f%%；判定：%s"
              % (100 * keep, "有独立信息，值得接入" if abs(t_net or 0) >= 2.0
                 else "不显著 → 只是放量涨跌的影子，用现成量价因子即可"))

    # ---------- ③ 换手率门槛是否有额外分辨力 ----------
    print("\n" + "=" * 96)
    print("③ 换手率门槛：在「已上榜」的票里，换手率还能再分吗？（决定规则写多细）")
    print("=" * 96)
    for label, lo, hi in (("≥30%", 30, 1e9), ("20-30%", 20, 30), ("15-20%", 15, 20), ("<15%", 0, 15)):
        lst = [(c, d) for (c, d), tr in evs.items() if lo <= tr < hi]
        per_day = defaultdict(list)
        allv = []
        for c, d in lst:
            H = 5
            f = fwd[H].get((c, d))
            if f is None or d not in dm_mean[H]:
                continue
            ex = f - dm_mean[H][d]
            per_day[d].append(ex)
            allv.append(ex)
        if len(allv) < 10:
            print("  %-10s n=%-4d 样本不足" % (label, len(allv)))
            continue
        dv = [statistics.fmean(v) for v in per_day.values() if v]
        print("  %-10s n=%-4d %+.2f%%  t=%s  胜率 %.1f%%"
              % (label, len(allv), statistics.fmean(allv), ft(tstat(dv)),
                 100.0 * sum(1 for v in allv if v > 0) / len(allv)))

    # ---------- ④ 与 avoid 重叠 ----------
    print("\n" + "=" * 96)
    print("④ 增量标的：最近交易日上榜票里，avoid 现有 6 条规则已经亮了多少灯？")
    print("=" * 96)
    try:
        import os
        import sys
        sys.path.insert(0, os.path.abspath("viewer"))
        sys.path.insert(0, os.path.abspath("."))
        import avoid
        last = cal[-1]
        codes = sorted({c for (c, d) in evs if d >= cal[-6]})
        if not codes:
            print("  最近 5 个交易日无上榜记录")
        else:
            res = avoid.compute(con, codes)
            got = [(c, res[c]) for c in codes if res.get(c)]
            bright = [x for x in got if x[1]["level"] in ("high", "mid")]
            print("  最近 5 个交易日上榜 %d 只，avoid 打分成功 %d 只" % (len(codes), len(got)))
            print("  avoid 已亮灯 %d 只（%.0f%%），未亮灯 %d 只 → 这些才是龙虎榜的增量"
                  % (len(bright), 100.0 * len(bright) / max(1, len(got)), len(got) - len(bright)))
            for c, s in got[:14]:
                fl = s.get("flags") or []
                flag = ",".join(x if isinstance(x, str) else str(x.get("name") or x.get("k") or x) for x in fl[:3]) or "-"
                print("    %s  分%-2d %-5s %s" % (c, s["score"], s["level"] or "-", flag))
    except Exception as e:  # noqa: BLE001
        print("  （跳过：%s）" % e)

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
