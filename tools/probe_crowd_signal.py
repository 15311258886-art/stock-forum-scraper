"""实盘/大V 信号可参考性实证（2026-10-30，进哥提问「淘股吧/雪球这批人的持仓与帖子有没有价值」）。

回答三个问题：
  ① 跟单实盘选手的 BUY（交割单）→ T+N 有没有超额？正还是负？
  ② 选手 SELL 之后标的是继续跌还是继续涨（衡量「卖出时点」质量）？
  ③ 论坛帖子的看多/看空情绪提及个股 → T+N 有没有超额？

口径与 tools/probe_avoid_signal.py 严格一致，便于横向比较：
  · 收益 = close[t+N] / close[t] - 1（t = 事件日对齐到当日或之后最近交易日）
  · 超额 ex = 个股收益 − 同日全市场均值（截面 demean，剔除大盘涨跌）
  · t 值：先按日聚合 ex 求日均，再对日均序列做 t 检验
    （不做日聚合会把同一天几千个样本当独立样本，t 值虚高几十倍）

已知天花板（写进结论，不掩饰）：
  · K 线表只到 2025-08-18 起，2025-08 之前的交割单测不了 → 样本集中在 2026-08~10。
  · 交割单 price 字段 99.9% 为空，只能用 K 线收盘价反推，无法复现「实际成交价」。
  · 选手若「先做后说」（涨完才晒单），T+N 会系统性偏正，这是晒单类数据无法根除的偏差。
    → 因此本脚本额外给一个「T+1 是否为负」的对照：若 T+1 就大正、T+5 反而回落，
       基本可判定是事后晒单而非事前信号。

用法：
  python tools/probe_crowd_signal.py
  python tools/probe_crowd_signal.py --start 2026-08-01
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from bisect import bisect_left
from collections import defaultdict

DB = "viewer/app.db"

HORIZONS = (1, 3, 5, 10)


# ----------------------------------------------------------------------
# 统计工具
# ----------------------------------------------------------------------
def tstat(vals):
    """时序 t 值（vals 为按日聚合后的日均值序列）。"""
    n = len(vals)
    if n < 5:
        return None
    m = statistics.fmean(vals)
    sd = statistics.stdev(vals)
    if sd == 0:
        return None
    return m / (sd / n ** 0.5)


def fmt_t(t):
    return "%.2f" % t if t is not None else "-"


def tag_of(t, expect_negative=False):
    """给结论打显著性标签。expect_negative=True 时负数才是「有用」。"""
    if t is None:
        return ""
    if abs(t) < 1.3:
        return ""
    if t <= -2.0:
        return " ★显著负"
    if t >= 2.0:
        return " ★显著正"
    return " ◇弱负" if t < 0 else " ◇弱正"


# ----------------------------------------------------------------------
# 数据装载
# ----------------------------------------------------------------------
def load_calendar(con):
    """全市场交易日列表（升序）。"""
    return [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]


def load_fwd(con, start):
    """{code: {date: {h: fwd_ret_pct}}}，以及每日全市场收益均值（用于 demean）。

    只保留 start 之后的日期，减少内存。
    """
    sql = """
    SELECT code, date, close,
           LEAD(close, %d) OVER (PARTITION BY code ORDER BY date) AS f%d
      FROM stock_indicators_daily
     WHERE date >= ?
    """ % (HORIZONS[0], HORIZONS[0])
    # 一次性取全窗口，避免多次扫表
    cols = ", ".join(
        "LEAD(close, %d) OVER (PARTITION BY code ORDER BY date) AS f%d" % (h, h)
        for h in HORIZONS)
    sql = ("SELECT code, date, close, %s FROM stock_indicators_daily "
           "WHERE date >= ?" % cols)

    fwd = defaultdict(dict)
    day_rets = {h: defaultdict(list) for h in HORIZONS}
    for row in con.execute(sql, (start,)):
        code, date, close = row[0], row[1], row[2]
        if not close or close <= 0:
            continue
        d = {}
        for i, h in enumerate(HORIZONS):
            fc = row[3 + i]
            if fc and fc > 0:
                r = (fc / close - 1.0) * 100.0
                d[h] = r
                day_rets[h][date].append(r)
        if d:
            fwd[code][date] = d
    day_mean = {h: {k: statistics.fmean(v) for k, v in m.items()}
                for h, m in day_rets.items()}
    return fwd, day_mean


def align(cal, date_str):
    """把任意日期对齐到「当日或之后最近的一个交易日」。返回 None 表示超出日历。"""
    if not date_str:
        return None
    d = date_str[:10]
    i = bisect_left(cal, d)
    return cal[i] if i < len(cal) else None


# ----------------------------------------------------------------------
# 事件抽取
# ----------------------------------------------------------------------
def events_trades(con, cal, start):
    """交割单事件：[(site, author, code, aligned_date, action)]"""
    out = []
    seen = set()
    for site, author, code, pt, action in con.execute(
            "SELECT site, author, symbol, post_time, action FROM trades "
            "WHERE post_time IS NOT NULL AND post_time <> '' "
            "  AND symbol IS NOT NULL AND symbol <> '' "
            "  AND action IN ('BUY','SELL','HOLD')"):
        d = align(cal, pt)
        if not d or d < start:
            continue
        # 同一人同一天对同一票的重复动作只算一次（避免刷屏帖子放大权重）
        k = (author, code, d, action)
        if k in seen:
            continue
        seen.add(k)
        out.append((site, author, code, d, action))
    return out


def events_posts(con, cal, start):
    """帖子情绪事件：[(site, author, code, aligned_date, sentiment)]"""
    out = []
    seen = set()
    sql = """
    SELECT p.site, p.author, m.symbol, p.post_time, p.sentiment
      FROM posts p JOIN stock_mentions m ON m.post_id_ref = p.id
     WHERE p.post_time IS NOT NULL AND p.post_time <> ''
       AND p.sentiment IN ('看多','看空')
       AND m.symbol IS NOT NULL AND m.symbol <> ''
    """
    for site, author, code, pt, senti in con.execute(sql):
        d = align(cal, pt)
        if not d or d < start:
            continue
        k = (author, code, d, senti)
        if k in seen:
            continue
        seen.add(k)
        out.append((site, author, code, d, senti))
    return out


# ----------------------------------------------------------------------
# 分组统计
# ----------------------------------------------------------------------
def run_stats(events, fwd, day_mean, label):
    """events: [(site, author, code, date, tag)]；返回 {key: 各 horizon 统计}"""
    res = {}
    for h in HORIZONS:
        acc = defaultdict(lambda: [0, 0.0, 0])          # key -> [n, sum, n_pos]
        dacc = defaultdict(lambda: defaultdict(list))   # key -> date -> [ex...]
        for site, author, code, d, tag in events:
            fd = fwd.get(code, {}).get(d)
            if not fd or h not in fd:
                continue
            dm = day_mean[h].get(d)
            if dm is None:
                continue
            ex = fd[h] - dm
            for key in (("ALL", label), (site, label), (tag, label)):
                a = acc[key]
                a[0] += 1
                a[1] += ex
                if ex > 0:
                    a[2] += 1
                dacc[key][d].append(ex)
        res[h] = {}
        for key, (n, tot, npos) in acc.items():
            dv = [statistics.fmean(v) for v in dacc[key].values() if len(v) >= 3]
            res[h][key] = {
                "n": n, "mean": tot / n, "t": tstat(dv),
                "win": 100.0 * npos / n, "days": len(dv),
            }
    return res


def print_block(res, title, keys_order=None):
    print("\n" + "=" * 96)
    print(title)
    print("=" * 96)
    hdr = "%-16s %6s" % ("分组", "n")
    for h in HORIZONS:
        hdr += "   T+%-2d超额  t值   胜率" % h
    print(hdr)
    print("-" * len(hdr))
    keys = keys_order or sorted(res[HORIZONS[-1]].keys())
    for key in keys:
        any_h = res[HORIZONS[0]].get(key) or res[HORIZONS[1]].get(key)
        if not any_h or any_h["n"] < 30:
            continue
        line = "%-16s %6d" % (key[0][:16], any_h["n"])
        for h in HORIZONS:
            v = res[h].get(key)
            if not v:
                line += "      -      -      -"
            else:
                line += "  %+7.3f%% %6s %5.1f%%" % (v["mean"], fmt_t(v["t"]), v["win"])
        print(line)


def corr_consensus(events, fwd, day_mean):
    """共识度：同一票同一天被几个不同作者提到 → 分组看超额是否随人数变化。"""
    print("\n" + "=" * 96)
    print("共识度检验（同一交易日 · 同一只票 · 被几个不同作者提到）—— 人越多是否越可信？")
    print("=" * 96)
    for h in (1, 5):
        cnt = defaultdict(set)
        for site, author, code, d, tag in events:
            cnt[(code, d, tag)].add(author)
        buckets = defaultdict(lambda: [0, 0.0, 0])
        dacc = defaultdict(lambda: defaultdict(list))
        for (code, d, tag), authors in cnt.items():
            fd = fwd.get(code, {}).get(d)
            if not fd or h not in fd:
                continue
            dm = day_mean[h].get(d)
            if dm is None:
                continue
            n = len(authors)
            b = "1人" if n == 1 else ("2人" if n == 2 else ("3-4人" if n <= 4 else "≥5人"))
            ex = fd[h] - dm
            a = buckets[b]
            a[0] += 1
            a[1] += ex
            if ex > 0:
                a[2] += 1
            dacc[b][d].append(ex)
        print("\n  T+%d：" % h)
        for b in ("1人", "2人", "3-4人", "≥5人"):
            if b not in buckets:
                continue
            n, tot, npos = buckets[b]
            dv = [statistics.fmean(v) for v in dacc[b].values() if len(v) >= 3]
            t = tstat(dv)
            print("    %-6s n=%-5d 超额 %+.3f%%  t=%s  胜率 %.1f%%%s"
                  % (b, n, tot / n, fmt_t(t), 100.0 * npos / n, tag_of(t)))


def author_split(events, fwd, day_mean, con, min_ev=8):
    """按作者历史活跃度分层：高频 vs 低频。检验「老手是不是更准」。"""
    print("\n" + "=" * 96)
    print("作者活跃度分层（作者在该源的事件总数）—— 高频选手比低频路人更准吗？")
    print("=" * 96)
    freq = defaultdict(int)
    for site, author, code, d, tag in events:
        freq[(site, author)] += 1
    for h in (1, 5):
        buckets = defaultdict(lambda: [0, 0.0, 0])
        dacc = defaultdict(lambda: defaultdict(list))
        for site, author, code, d, tag in events:
            fd = fwd.get(code, {}).get(d)
            if not fd or h not in fd:
                continue
            dm = day_mean[h].get(d)
            if dm is None:
                continue
            n_ev = freq[(site, author)]
            b = "≤2次" if n_ev <= 2 else ("3-7次" if n_ev <= 7 else ("8-19次" if n_ev <= 19 else "≥20次"))
            ex = fd[h] - dm
            a = buckets[b]
            a[0] += 1
            a[1] += ex
            if ex > 0:
                a[2] += 1
            dacc[b][d].append(ex)
        print("\n  T+%d：" % h)
        for b in ("≤2次", "3-7次", "8-19次", "≥20次"):
            if b not in buckets:
                continue
            n, tot, npos = buckets[b]
            dv = [statistics.fmean(v) for v in dacc[b].values() if len(v) >= 3]
            t = tstat(dv)
            print("    %-7s n=%-5d 超额 %+.3f%%  t=%s  胜率 %.1f%%%s"
                  % (b, n, tot / n, fmt_t(t), 100.0 * npos / n, tag_of(t)))


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-01", help="事件起始日（K线从 2025-08-18 起）")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    cal = load_calendar(con)
    print("交易日历：%s .. %s（%d 日）" % (cal[0], cal[-1], len(cal)))

    print("[1/4] 装载前向收益 …", flush=True)
    fwd, day_mean = load_fwd(con, args.start)
    print("     覆盖股票 %d 只" % len(fwd))

    print("[2/4] 抽取交割单事件 …", flush=True)
    ev_t = events_trades(con, cal, args.start)
    print("     事件 %d 条（去重后）" % len(ev_t))
    by = defaultdict(int)
    for site, author, code, d, tag in ev_t:
        by[(tag, site)] += 1
    for k in sorted(by, key=lambda x: -by[x])[:12]:
        print("       %-8s %-12s %d" % (k[0], k[1], by[k]))

    print("[3/4] 抽取帖子情绪事件 …", flush=True)
    ev_p = events_posts(con, cal, args.start)
    print("     事件 %d 条（去重后）" % len(ev_p))

    print("[4/4] 统计 …", flush=True)

    # ---- 交割单：BUY（做多信号）----
    buy = [e for e in ev_t if e[4] == "BUY"]
    sell = [e for e in ev_t if e[4] == "SELL"]
    hold = [e for e in ev_t if e[4] == "HOLD"]

    if buy:
        r = run_stats(buy, fwd, day_mean, "BUY")
        print_block(r, "① 实盘选手 BUY（交割单买入）→ T+N 相对全市场超额（正=跟单赚钱）")
    if sell:
        r = run_stats(sell, fwd, day_mean, "SELL")
        print_block(r, "② 实盘选手 SELL（交割单卖出）→ 卖出后 T+N 超额（负=卖对了，正=卖早了）")
    if hold:
        r = run_stats(hold, fwd, day_mean, "HOLD")
        print_block(r, "③ 实盘选手 HOLD（持仓不动）→ T+N 超额")
    if ev_p:
        r = run_stats(ev_p, fwd, day_mean, "POST")
        print_block(r, "④ 论坛帖子情绪提及 → T+N 超额（看多正=情绪有效，看空负=情绪有效）")

    # ---- 共识度 ----
    if buy:
        print("\n\n>>> 交割单 BUY 的共识度")
        corr_consensus(buy, fwd, day_mean)
    if ev_p:
        print("\n\n>>> 帖子提及的共识度（按情绪分别看）")
        for s in ("看多", "看空"):
            sub = [e for e in ev_p if e[4] == s]
            if sub:
                print("\n--- %s ---" % s)
                corr_consensus(sub, fwd, day_mean)

    # ---- 作者活跃度 ----
    if buy:
        print("\n\n>>> 交割单 BUY 的作者活跃度分层")
        author_split(buy, fwd, day_mean, con)

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
