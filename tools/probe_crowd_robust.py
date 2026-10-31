"""大V/实盘信号稳健性检验（2026-10-30）。

对一个"有意义"的信号，单看全样本 t 值是不够的：
  · 样本高度集中在一两个月，全样本 t 可能来自某一段行情；
  · 相邻交易日的样本是同一批人、重叠持仓，自相关强 → t 值虚高；
  · 换个分组就反号，说明是极端日主导。

本脚本做三件事：
  ① 时间对半切（前 50% / 后 50%），看两半是否同号 —— 唯一能挡「行情噪声」的检验；
  ② 按周聚合（而非按日）再算 t —— 降低同日重叠持仓造成的自相关；
  ③ 单日剔除法：逐个去掉样本最多的 3 天，看结论是否翻转 —— 检验是否被极端日绑架。

用法：python tools/probe_crowd_robust.py
"""
from __future__ import annotations

import sqlite3
import statistics
from bisect import bisect_left
from collections import defaultdict

DB = "viewer/app.db"
START = "2026-08-01"
H = 5                      # 统一检验 T+5（决策周期）


def tstat(vals):
    n = len(vals)
    if n < 5:
        return None
    m = statistics.fmean(vals)
    sd = statistics.stdev(vals)
    if sd == 0:
        return None
    return m / (sd / n ** 0.5)


def ft(t):
    return "%.2f" % t if t is not None else "-"


def load():
    con = sqlite3.connect(DB)
    cal = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]

    # 前向 T+5 收益 + 每日全市场均值
    rows = con.execute("""
        SELECT code, date, close,
               LEAD(close, 5) OVER (PARTITION BY code ORDER BY date) f5
          FROM stock_indicators_daily WHERE date >= ?
    """, (START,)).fetchall()
    fwd, day_rets = {}, defaultdict(list)
    for code, d, close, f5 in rows:
        if not close or close <= 0 or not f5 or f5 <= 0:
            continue
        r = (f5 / close - 1.0) * 100.0
        fwd.setdefault(code, {})[d] = r
        day_rets[d].append(r)
    day_mean = {k: statistics.fmean(v) for k, v in day_rets.items()}

    def align(s):
        i = bisect_left(cal, s[:10])
        return cal[i] if i < len(cal) else None

    # 交割单
    ev = []
    seen = set()
    for site, author, sym, pt, act in con.execute(
            "SELECT site,author,symbol,post_time,action FROM trades "
            "WHERE post_time<>'' AND symbol<>'' AND action IN ('BUY','SELL','HOLD')"):
        d = align(pt)
        if not d or d < START:
            continue
        k = (author, sym, d, act)
        if k in seen:
            continue
        seen.add(k)
        ev.append((site, author, sym, d, act))

    # 帖子情绪
    evp = []
    seen = set()
    for site, author, sym, pt, se in con.execute("""
            SELECT p.site,p.author,m.symbol,p.post_time,p.sentiment
              FROM posts p JOIN stock_mentions m ON m.post_id_ref=p.id
             WHERE p.post_time<>'' AND p.sentiment IN ('看多','看空') AND m.symbol<>''"""):
        d = align(pt)
        if not d or d < START:
            continue
        k = (author, sym, d, se)
        if k in seen:
            continue
        seen.add(k)
        evp.append((site, author, sym, d, se))

    con.close()
    return fwd, day_mean, ev, evp


def collect(events, fwd, day_mean):
    """[(date, ex)]，只保留 T+5 可算的。"""
    out = []
    for site, author, code, d, tag in events:
        r = fwd.get(code, {}).get(d)
        if r is None:
            continue
        dm = day_mean.get(d)
        if dm is None:
            continue
        out.append((d, r - dm))
    return out


def agg_daily(pairs, min_n=3):
    g = defaultdict(list)
    for d, ex in pairs:
        g[d].append(ex)
    return [(d, statistics.fmean(v), len(v)) for d, v in sorted(g.items()) if len(v) >= min_n]


def report(name, pairs):
    if len(pairs) < 40:
        print("  %-22s 样本不足 (%d)" % (name, len(pairs)))
        return
    daily = agg_daily(pairs)
    if len(daily) < 8:
        print("  %-22s 有效交易日不足 (%d 日)" % (name, len(daily)))
        return
    allv = [x[1] for x in daily]
    t_all = tstat(allv)

    # ① 时间对半切
    mid = len(daily) // 2
    a, b = allv[:mid], allv[mid:]
    t_a, t_b = tstat(a), tstat(b)
    ma = statistics.fmean(a)
    mb = statistics.fmean(b)
    same_sign = (ma > 0) == (mb > 0)

    # ② 按周聚合
    wk = defaultdict(list)
    for d, m, n in daily:
        wk[d[:8] + "%02d" % ((int(d[8:10]) - 1) // 7)].append(m)
    wkv = [statistics.fmean(v) for v in wk.values()]
    t_wk = tstat(wkv)

    # ③ 单日剔除（去掉样本最多的 3 天）
    top3 = sorted(daily, key=lambda x: -x[2])[:3]
    bad = {d for d, _, _ in top3}
    kept = [m for d, m, n in daily if d not in bad]
    t_drop = tstat(kept)

    print("  %-22s n=%-5d 日数=%-3d 全样本 %+7.3f%% t=%5s | 前半 %+6.3f%% t=%5s 后半 %+6.3f%% t=%5s %s | 周聚合 t=%5s | 剔Top3日 %+6.3f%% t=%5s"
          % (name, len(pairs), len(daily), statistics.fmean(allv), ft(t_all),
             ma, ft(t_a), mb, ft(t_b), "同号" if same_sign else "★反号",
             ft(t_wk), statistics.fmean(kept) if kept else 0, ft(t_drop)))


def main():
    fwd, day_mean, ev, evp = load()
    print("T+5 稳健性检验（样本 %s 起；超额=个股−同日全市场均值；均按日聚合后检验）\n" % START)

    print("【交割单】")
    for act in ("BUY", "SELL", "HOLD"):
        sub = [e for e in ev if e[4] == act]
        if sub:
            report("全部 · %s" % act, collect(sub, fwd, day_mean))
            for site in ("taoguba", "eastmoney"):
                s2 = [e for e in sub if e[0] == site]
                if len(s2) >= 40:
                    report("  %s · %s" % (site, act), collect(s2, fwd, day_mean))

    print("\n【论坛情绪】")
    for se in ("看多", "看空"):
        sub = [e for e in evp if e[4] == se]
        if sub:
            report("全部 · %s" % se, collect(sub, fwd, day_mean))
            for site in ("taoguba", "eastmoney", "xueqiu", "sina"):
                s2 = [e for e in sub if e[0] == site]
                if len(s2) >= 40:
                    report("  %s · %s" % (site, se), collect(s2, fwd, day_mean))

    print("\n【作者集中度】—— 前 3 名作者占总事件比例（>40% 说明样本被少数人主导）")
    for act in ("BUY", "SELL", "HOLD"):
        sub = [e for e in ev if e[4] == act]
        if not sub:
            continue
        cnt = defaultdict(int)
        for e in sub:
            cnt[e[1]] += 1
        top = sorted(cnt.values(), reverse=True)
        share = 100.0 * sum(top[:3]) / len(sub)
        print("  %-6s 作者%3d 人 · 前3名占 %.0f%%（%s）"
              % (act, len(cnt), share, "★被少数人主导" if share > 40 else "较分散"))


if __name__ == "__main__":
    main()
