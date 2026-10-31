# -*- coding: utf-8 -*-
"""避坑指标实证探针（2026-10-30）。

背景：进哥判定「博主反向警示」不能拿沉淀数据绑定个股——6 月的推荐到 11 月早已失效，
荐股保鲜期约 20 天内。避坑功能要留，但**指标不能由博主推荐来提供**。

本探针回答一个问题：在**每日更新、全市场覆盖**的数据源里，哪些「当日收盘即可观测」
的状态，对 T+5 收益有稳定且显著的负向预示？只认实证，不认直觉。

数据源（均每日更新，非沉淀快照）：
  · stock_indicators_daily  58 列技术指标，全市场 5309 只，最新 2026-10-29
  · stock_ann               公告，含 tone(bad/good/neutral) 与 cat，最新 2026-10-30

口径：
  · r5     = close[T+5] / close[T] - 1        （横截面同日对齐）
  · ex5    = r5 - 当日全市场 r5 均值            （剔除大盘 beta，只看相对）
  · 指标命中 → 统计 n / 均值 ex5 / t 值 / 胜率(超额>0 占比)
  · t 值按「按日聚合后再做时序 t 检验」算，避免同一天几千个样本造成伪显著
    （与项目既有 signal_backtest 口径一致：有效独立样本 ≈ 交易日数，不是股票数）

用法：python tools/probe_avoid_signal.py [--start 2026-04-01]
"""
import argparse
import os
import sqlite3
import statistics
import sys
from collections import deque
from datetime import date as _date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("VIEWER_DB") or os.path.join(BASE, "viewer", "app.db")

(C_CODE, C_DATE, C_CLOSE, C_MA20, C_MA60, C_MACDH, C_PMACDH,
 C_KDJK, C_KDJD, C_PKDJK, C_PKDJD, C_BIAS6, C_RSI6, C_VR, C_ATR,
 C_PCT, C_PCT2, C_R5) = range(18)

BUILD_SQL = """
SELECT code, date, close, ma20, ma60, macdh,
       LAG(macdh, 1) OVER w,
       kdjk, kdjd, LAG(kdjk, 1) OVER w, LAG(kdjd, 1) OVER w,
       bias6, rsi6, volume_ratio, atr,
       CASE WHEN p1 > 0 THEN (close / p1 - 1.0) * 100.0 END,
       CASE WHEN p2 > 0 THEN (close / p2 - 1.0) * 100.0 END,
       CASE WHEN close > 0 THEN (f5 / close - 1.0) * 100.0 END
FROM (
  SELECT code, date, close, ma20, ma60, macdh, kdjk, kdjd, bias6, rsi6,
         volume_ratio, atr,
         LEAD(close, 5) OVER w AS f5,
         LAG(close, 1)  OVER w AS p1,
         LAG(close, 2)  OVER w AS p2
  FROM stock_indicators_daily
  WHERE date >= ?1
  WINDOW w AS (PARTITION BY code ORDER BY date)
)
WHERE f5 IS NOT NULL
WINDOW w AS (PARTITION BY code ORDER BY date)
ORDER BY code, date
"""


def _tstat(vals):
    n = len(vals)
    if n < 5:
        return None
    mu = statistics.fmean(vals)
    sd = statistics.stdev(vals)
    if sd == 0:
        return None
    return mu / (sd / (n ** 0.5))


RULE_KEYS = [
    "当日跌>=5%", "当日跌3~5%", "连跌2日", "两日累计跌>=6%",
    "破MA20", "破MA20且破MA60", "MA20下方偏离>10%", "MA60上方偏离>20%(过热)",
    "20日高点回撤>=15%", "60日高点回撤>=25%", "创20日新高",
    "MACD死叉", "KDJ高位死叉", "RSI6超买>85", "BIAS6超买>10",
    "放量下跌(量比>=1.8且跌>=3%)", "缩量下跌(量比<=0.6且跌<0)", "ATR占价>6%(高波动)",
]


def mask_of(r, hh20, hh60):
    """返回 18 位命中掩码。位序与 RULE_KEYS 一致。只用当日及以前数据，无未来函数。"""
    c = r[C_CLOSE]
    pct = r[C_PCT]
    pct2 = r[C_PCT2]
    ma20 = r[C_MA20]
    ma60 = r[C_MA60]
    m = 0
    if pct is not None:
        if pct <= -5:
            m |= 1
        elif pct <= -3:
            m |= 2
        if pct2 is not None:
            if pct < 0 and pct2 < 0:
                m |= 4
            if pct + pct2 <= -6:
                m |= 8
    if ma20:
        brk20 = c < ma20
        if brk20:
            m |= 16
            if ma60 and c < ma60:
                m |= 32
            if c < ma20 * 0.9:
                m |= 64
    if ma60 and ma60 > 0 and c > ma60 * 1.2:
        m |= 128
    if hh20:
        if c <= hh20 * 0.85:
            m |= 256
        if c >= hh20:
            m |= 1024
    if hh60 and c <= hh60 * 0.75:
        m |= 512
    macdh, pmacdh = r[C_MACDH], r[C_PMACDH]
    if macdh is not None and pmacdh is not None and pmacdh >= 0 and macdh < 0:
        m |= 2048
    k, d, pk, pd_ = r[C_KDJK], r[C_KDJD], r[C_PKDJK], r[C_PKDJD]
    if None not in (k, d, pk, pd_) and pk >= pd_ and k < d and k > 70:
        m |= 4096
    if r[C_RSI6] is not None and r[C_RSI6] > 85:
        m |= 8192
    if r[C_BIAS6] is not None and r[C_BIAS6] > 10:
        m |= 16384
    vr = r[C_VR]
    if vr is not None and pct is not None:
        if vr >= 1.8 and pct <= -3:
            m |= 32768
        if vr <= 0.6 and pct < 0:
            m |= 65536
    atr = r[C_ATR]
    if atr and c > 0 and atr / c > 0.06:
        m |= 131072
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-04-01")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    print("[1/4] 取数（LEAD/LAG 走索引）…")
    rows = con.execute(BUILD_SQL, (args.start,)).fetchall()
    print("      样本 %d 行" % len(rows))
    if not rows:
        print("无样本，检查日期区间")
        return 1

    print("[2/4] 滚动极值（单调队列 O(n)）+ 横截面 demean …", flush=True)
    n = len(rows)
    hh20 = [0.0] * n
    hh60 = [0.0] * n
    day_agg = {}
    last_code = None
    pos = 0
    mq20 = deque()
    mq60 = deque()
    for i in range(n):
        r = rows[i]
        code, c = r[C_CODE], r[C_CLOSE]
        if code != last_code:
            last_code, pos = code, 0
            mq20.clear()
            mq60.clear()
        while mq20 and mq20[-1][1] <= c:
            mq20.pop()
        mq20.append((pos, c))
        while mq20[0][0] <= pos - 20:
            mq20.popleft()
        while mq60 and mq60[-1][1] <= c:
            mq60.pop()
        mq60.append((pos, c))
        while mq60[0][0] <= pos - 60:
            mq60.popleft()
        hh20[i] = mq20[0][1]
        hh60[i] = mq60[0][1]
        pos += 1
        d = r[C_DATE]
        a = day_agg.get(d)
        if a is None:
            day_agg[d] = [r[C_R5], 1]
        else:
            a[0] += r[C_R5]
            a[1] += 1
    day_mean = {d: v[0] / v[1] for d, v in day_agg.items()}
    print("      交易日 %d 个；样本期 %s .. %s"
          % (len(day_mean), min(day_mean), max(day_mean)), flush=True)

    print("[3/4] 逐规则统计（技术位掩码 + 公告窗口）…", flush=True)
    n_all = n
    keys = RULE_KEYS
    acc = [[0, 0.0, 0] for _ in keys]          # n / sum(ex5) / n(ex5>0)
    dacc = [{} for _ in keys]                   # date -> [n, sum]
    dm_get = day_mean.get

    # ---- 利空公告（tone=bad）：按 code 预存 (ordinal, cat)，主循环内用指针推进 ----
    # 为什么要带 cat：第一版只按窗口统计 ALL_BAD（t=-3.07），
    # 按类别拆开后才发现它是被「质押担保」这类流程公告稀释的混合信号 ——
    # 股东减持 t=-5.66、人事变动 t=-3.33，而监管处罚 / 风险提示 / 诉讼 都是**正向**。
    # 所以这里必须分类统计，否则「利空公告有效」这个结论会误导落地实现。
    _oc = {}

    def ordn(s):
        v = _oc.get(s)
        if v is None:
            v = _oc[s] = _date.fromisoformat(s).toordinal()
        return v

    bad_by_code = {}
    for code, nd, cat in con.execute(
            "SELECT code, notice_date, cat FROM stock_ann WHERE tone='bad'"):
        if code and nd:
            bad_by_code.setdefault(code, []).append((ordn(nd), cat or "未分类"))
    for v in bad_by_code.values():
        v.sort()
    AW = 15                                      # 3/7 日窗口实证不显著，只留 15 日
    a_acc = {}                                   # cat -> [n, sum, npos]
    a_dacc = {}                                  # cat -> {date: [n, sum]}

    last_code2 = None
    blist = ()
    bp = 0
    for i in range(n):
        r = rows[i]
        code = r[C_CODE]
        if code != last_code2:
            last_code2 = code
            blist = bad_by_code.get(code) or ()
            bp = 0
        d = r[C_DATE]
        ex = r[C_R5] - dm_get(d)
        # 公告维度（与技术规则独立，不受位掩码 continue 影响）
        do = ordn(d)
        while bp < len(blist) and blist[bp][0] <= do:
            bp += 1
        if bp:
            gap = do - blist[bp - 1][0]
            if gap <= AW:
                cat = blist[bp - 1][1]
                for key in (cat, "ALL_BAD"):
                    a = a_acc.get(key)
                    if a is None:
                        a = a_acc[key] = [0, 0.0, 0]
                    a[0] += 1
                    a[1] += ex
                    if ex > 0:
                        a[2] += 1
                    da = a_dacc.get(key)
                    if da is None:
                        da = a_dacc[key] = {}
                    dd = da.get(d)
                    if dd is None:
                        da[d] = [1, ex]
                    else:
                        dd[0] += 1
                        dd[1] += ex
        m = mask_of(r, hh20[i], hh60[i])
        if not m:
            continue
        while m:
            low = m & -m
            j = low.bit_length() - 1
            m ^= low
            a = acc[j]
            a[0] += 1
            a[1] += ex
            if ex > 0:
                a[2] += 1
            dd = dacc[j].get(d)
            if dd is None:
                dacc[j][d] = [1, ex]
            else:
                dd[0] += 1
                dd[1] += ex

    print("[4/4] 结果（按 |t| 排序；t 用日聚合时序口径）\n")
    hdr = "%-30s %8s %7s %10s %8s %7s" % ("规则", "命中数", "占比", "ex5均值%", "日t值", "胜率%")
    print(hdr)
    print("-" * (len(hdr) + 10))
    res = []
    for j, k in enumerate(keys):
        cnt, tot, npos = acc[j]
        if cnt < 300:
            res.append((0.0, k, cnt, None, None, None))
            continue
        dvals = [v[1] / v[0] for v in dacc[j].values() if v[0] >= 5]
        t = _tstat(dvals)
        res.append((abs(t) if t else 0.0, k, cnt, tot / cnt, t, 100.0 * npos / cnt))
    for _, k, n, mu, t, win in sorted(res, reverse=True):
        if mu is None:
            print("%-30s %8d  (样本不足)" % (k, n))
            continue
        tag = " ★显著负(避坑用)" if (t is not None and t <= -2.0) else (
              " ◇轻微负" if (t is not None and t <= -1.3) else (
              " ☆显著正" if (t is not None and t >= 2.0) else ""))
        print("%-30s %8d %6.2f%% %10.3f %8.2f %7.1f%s"
              % (k, n, 100.0 * n / n_all, mu, t, win, tag))

    print("\n[公告 tone=bad · 近 %d 日 · 按 cat 分组]（按最近一条的类别归户）" % AW)
    print("%-14s %7s %8s %10s %8s %7s" % ("cat", "命中数", "占比", "ex5均值%", "日t值", "胜率%"))
    print("-" * 60)
    rows_a = []
    for cat, (cnt, tot, npos) in a_acc.items():
        if cnt < 60:
            rows_a.append((0.0, cat, cnt, None, None, None))
            continue
        dvals = [v[1] / v[0] for v in a_dacc[cat].values() if v[0] >= 5]
        tt = _tstat(dvals)
        rows_a.append((abs(tt) if tt else 0.0, cat, cnt, tot / cnt, tt,
                       100.0 * npos / cnt))
    for _, cat, cnt, mu, tt, wrate in sorted(rows_a, reverse=True):
        if mu is None:
            print("%-14s %7d  (样本不足)" % (cat, cnt))
            continue
        tag = " ★显著负(避坑用)" if (tt is not None and tt <= -2.0) else (
              " ◇轻微负" if (tt is not None and tt <= -1.3) else (
              " ☆正向(不采用)" if (tt is None or tt >= 0) else ""))
        print("%-14s %7d %7.2f%% %10.3f %8s %7.1f%s"
              % (cat, cnt, 100.0 * cnt / n_all, mu,
                 ("%.2f" % tt) if tt is not None else "-", wrate, tag))
    print("\n  采用规则：ANN_CATS = ('股东减持','人事变动')；"
          "其余类别（质押担保/减值计提/监管处罚/风险提示/诉讼仲裁/资产冻结）一律不算。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
