# -*- coding: utf-8 -*-
"""博主战绩评分（2026-10-29 新增，对应「⑥ 接入博主战绩」）。

## 为什么需要这一层

`resonance.py::_blogger_credibility` 原来直接用 `AVG(blogger_perf.r5)` 定信誉档。
2026-10-29 实测这批数据：r5 均值 **-3.29%**、兑现率 **29.8%**（n=954）。
但这个数字不能读作「博主水平差」——样本期 2026-08~10 全市场处在弱势
（同期 T+5 基线为负），所有博主一起吃亏。绝对收益混进了市场 beta，必须减基线。

## 三个必须修的方法学问题

1. **不超额** → 改成「个股 r5 − 该推荐日全市场 T+5 均值」。基线用
   `stock_indicators_daily` 自算，与 r5 同口径（推荐日收盘 → 第 5 个交易日收盘）。

2. **独立观测不是「博主×股票」** → 实测同一博主单日最多推荐 56 只
   （土豆看财报 2026-10-19），同一天推的所有票共享同一市场因子，把它们当
   独立样本会把 n 从 ~50 吹到 ~1000。本模块一律先按 **(博主, 推荐日) 聚合**
   成 1 个观测（当日平均超额），再对观测做统计。

3. **小样本直接定档** → 1 条记录的博主也会被算出 ±1.5 系数。改为向 0 收缩：
   `shrunk = edge * n_days / (n_days + K)`，K=8（不足 8 天衰减一半）。

## 输出

`blogger_score` 表（每博主一行）+ `blogger_score_meta`（基线口径、样本期、
有效独立窗口数）。评级：
    A 可参考   n_days≥8 且收缩超额 ≥+0.50% 且前后半段同为正
    C 劣势     n_days≥8 且收缩超额 ≤-0.50% 且前后半段同为负
    D 样本不足 n_days < 8
    B 观察     其余
批量扫单（日均推荐只数中位数 > 30）最高只能给 B —— 推 50 只等于撒网，
选股信息量接近零，即使收益好看也不代表能力。

用法：
    python -m viewer.blogger_score            # 计算并落库
    python -m viewer.blogger_score --show     # 计算后打印明细
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "viewer", "app.db")

FWD = 5                 # 与 blogger_perf.r5 同口径
SHRINK_K = 8            # 收缩强度：n_days=8 时衰减一半
MIN_DAYS = 8            # 低于此天数判 D
EDGE_A = 0.50           # 评级阈值（百分点，已超额化）
BULK_MEDIAN = 30        # 日均推荐只数中位数超过此值 → 判批量扫单


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS blogger_score (
        blogger     TEXT PRIMARY KEY,
        n_stocks    INTEGER,     -- 原始推荐条数
        n_days      INTEGER,     -- 独立观测数（博主×推荐日）
        med_per_day REAL,        -- 日均推荐只数中位数（识别批量扫单）
        edge_mean   REAL,        -- 日度平均超额（百分点）
        edge_med    REAL,
        edge_shrunk REAL,        -- 收缩后超额（实际用于定档）
        t_stat      REAL,
        win_days    REAL,        -- 超额>0 的交易日占比
        edge_h1     REAL,        -- 前半段日均超额
        edge_h2     REAL,        -- 后半段日均超额
        stab        TEXT,        -- ok / bad / unstable / insufficient
        grade       TEXT,        -- A / B / C / D
        cred        REAL,        -- 信誉系数 0.5~1.5（给 resonance 用）
        period_from TEXT,
        period_to   TEXT,
        updated     TEXT
    );
    CREATE TABLE IF NOT EXISTS blogger_score_meta (
        k TEXT PRIMARY KEY,
        v TEXT,
        updated TEXT
    );
    """)
    con.commit()


def _base_date_of(rec_date: str, trade_dates: list) -> str | None:
    """rec_date → 「≤ rec_date 的最后一个交易日」。

    必须与 blogger_perf._rows_for 的基点口径完全一致：
        for d, c in k:
            if d <= str(rec_date): base_d, base_c = d, c
            else: break
    即周末/节假日发布的推荐，基点落在**上一个交易日收盘**。
    （这本身是个可执行性问题：周五收盘时消息还没发，买不到。本模块单独量化它。）
    """
    lo, hi, ans = 0, len(trade_dates) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if trade_dates[mid] <= rec_date:
            ans = trade_dates[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _daily_baseline(con: sqlite3.Connection, dates: list) -> tuple:
    """返回 (rec_date → 基线百分数, 交易日列表)。

    基线口径与 blogger_perf.r5 严格对齐：基点日收盘 → 第 FWD 个交易日收盘，
    对当日**全部有收盘价的股票**取算均，即「同期全市场 T+5 平均水平」。
    用 stock_indicators_daily（本地全市场 close，不依赖出网）。
    """
    trade_dates = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]
    if not trade_dates:
        return {}, []
    idx = {d: i for i, d in enumerate(trade_dates)}
    need = sorted({d for d in dates if d})
    mapped = {}
    for d in need:
        bd = _base_date_of(d, trade_dates)
        if bd is not None:
            mapped[d] = bd
    if not mapped:
        return {}, trade_dates
    base_days = sorted(set(mapped.values()))
    lo = max(0, idx[base_days[0]] - 1)
    hi = idx[trade_dates[-1]]
    win = trade_dates[lo:hi + 1]
    if len(win) < FWD + 2:
        return {}, trade_dates

    pos = {d: i for i, d in enumerate(win)}
    closes = defaultdict(lambda: [None] * len(win))
    q = ("SELECT code, date, close FROM stock_indicators_daily"
         " WHERE date >= ? AND date <= ? AND close > 0")
    for code, dt, cl in con.execute(q, (win[0], trade_dates[-1])):
        p = pos.get(dt)
        if p is not None:
            closes[code][p] = cl

    per_day = {}
    for bd in base_days:
        i = pos.get(bd)
        if i is None or i + FWD >= len(win):
            continue
        agg, k = 0.0, 0
        for arr in closes.values():
            c0, c1 = arr[i], arr[i + FWD]
            if c0 and c1:
                agg += c1 / c0 - 1.0
                k += 1
        if k >= 500:                      # 覆盖面门槛，避免半截数据污染基线
            per_day[bd] = agg / k * 100.0  # 百分数，与 r5 同单位

    return {d: per_day[bd] for d, bd in mapped.items() if bd in per_day}, trade_dates


def _stats(vals: list) -> dict:
    n = len(vals)
    if not n:
        return {"n": 0}
    m = sum(vals) / n
    if n > 1:
        var = sum((x - m) ** 2 for x in vals) / (n - 1)
        sd = math.sqrt(var)
        t = m / (sd / math.sqrt(n)) if sd > 0 else 0.0
    else:
        sd, t = 0.0, 0.0
    sv = sorted(vals)
    med = sv[n // 2] if n % 2 else (sv[n // 2 - 1] + sv[n // 2]) / 2.0
    return {"n": n, "mean": m, "med": med, "sd": sd, "t": t,
            "win": sum(1 for x in vals if x > 0) / float(n)}


def compute(con: sqlite3.Connection, show: bool = False) -> dict:
    t0 = time.time()
    rows = con.execute(
        "SELECT blogger, rec_date, stock_code, r5 FROM blogger_perf"
        " WHERE r5 IS NOT NULL AND rec_date IS NOT NULL AND rec_date <> ''"
        "   AND stock_code IS NOT NULL AND stock_code <> ''").fetchall()
    if not rows:
        return {"ok": False, "err": "blogger_perf 无可用样本"}

    base, trade_dates = _daily_baseline(con, [r["rec_date"] for r in rows])
    if not base:
        return {"ok": False, "err": "无法计算日度基线"}

    # (blogger, date) -> [超额列表]；同时记录「基点滞后」用于量化可执行性偏差
    grid = defaultdict(list)
    skipped = 0
    ex_ontime, ex_late = [], []      # 交易日发布 vs 非交易日发布（后者买不到）
    for r in rows:
        b = base.get(r["rec_date"])
        if b is None:
            skipped += 1
            continue
        e = r["r5"] - b
        grid[(r["blogger"], r["rec_date"])].append(e)
        bd = _base_date_of(r["rec_date"], trade_dates)
        if bd and bd == r["rec_date"]:
            ex_ontime.append(e)
        else:
            ex_late.append(e)

    per = defaultdict(lambda: {"days": [], "n_stocks": 0, "per_day": [], "dates": []})
    for (bg, dt), exs in grid.items():
        e = sum(exs) / len(exs)
        per[bg]["days"].append(e)
        per[bg]["n_stocks"] += len(exs)
        per[bg]["per_day"].append(len(exs))
        per[bg]["dates"].append((dt, e))

    ups, out = [], []
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for bg, d in per.items():
        s = _stats(d["days"])
        n_days = s["n"]
        med_day = _median(d["per_day"])
        shrunk = s["mean"] * n_days / (n_days + SHRINK_K) if n_days else 0.0
        # 前后半段：按日期排序切半分（与 signal_backtest 同口径）
        ds = sorted(d["dates"])
        half = len(ds) // 2
        h1 = _stats([x[1] for x in ds[:half]]) if half >= 3 else {"mean": None}
        h2 = _stats([x[1] for x in ds[half:]]) if len(ds) - half >= 3 else {"mean": None}
        e1, e2 = h1.get("mean"), h2.get("mean")

        if e1 is None or e2 is None:
            stab = "insufficient"
        elif e1 > 0 and e2 > 0:
            stab = "ok"
        elif e1 < 0 and e2 < 0:
            stab = "bad"
        else:
            stab = "unstable"

        if n_days < MIN_DAYS:
            grade = "D"
        elif stab == "ok" and shrunk >= EDGE_A and s["t"] >= 2.0:
            grade = "A"
        elif stab == "bad" and shrunk <= -EDGE_A and s["t"] <= -2.0:
            grade = "C"
        else:
            grade = "B"
        if med_day > BULK_MEDIAN and grade in ("A", "C"):
            grade = "B"          # 批量扫单不给极端评级（推 50 只等于撒网）

        # 信誉系数：收缩超额 × 证据强度折减。
        # 只按 shrunk 给权重是不够的——2026-10-29 实测所有博主 |t| < 1.2（无显著性），
        # 直接乘 0.15 会给出 +27% 的加成，等于用噪音放大仓位。折减因子 |t|/2
        # 让「不显著」自动退回中性 1.0，显著性越高才越允许偏离。
        strength = min(1.0, abs(s["t"]) / 2.0) if n_days >= MIN_DAYS else 0.0
        if grade == "D":
            cred = 1.0
        else:
            cred = max(0.5, min(1.5, 1.0 + shrunk * 0.15 * strength))

        out.append((bg, d["n_stocks"], n_days, round(med_day, 1), round(s["mean"], 4),
                    round(s["med"], 4), round(shrunk, 4), round(s["t"], 2),
                    round(s["win"], 4),
                    None if e1 is None else round(e1, 4),
                    None if e2 is None else round(e2, 4),
                    stab, grade, round(cred, 3),
                    ds[0][0], ds[-1][0], now))
        ups.append(out[-1])

    con.executemany(
        "INSERT OR REPLACE INTO blogger_score"
        "(blogger,n_stocks,n_days,med_per_day,edge_mean,edge_med,edge_shrunk,t_stat,"
        " win_days,edge_h1,edge_h2,stab,grade,cred,period_from,period_to,updated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ups)
    so, sl = _stats(ex_ontime), _stats(ex_late)
    meta = {
        "fwd": str(FWD), "shrink_k": str(SHRINK_K), "min_days": str(MIN_DAYS),
        "n_obs_rows": str(len(rows)), "n_skipped_no_base": str(skipped),
        "n_bloggers": str(len(out)), "baseline_days": str(len(base)),
        "period_from": min(r["rec_date"] for r in rows),
        "period_to": max(r["rec_date"] for r in rows),
        "eff_windows": "%.1f" % (len(base) / float(FWD) if base else 0),
        # 可执行性：非交易日发布的推荐，基点是「上一个交易日收盘」——那一刻消息还没出，
        # 按这个价买入是不可能的。两者超额之差就是「前视偏差」的量级。
        "n_ontime": str(so.get("n", 0)),
        "n_late": str(sl.get("n", 0)),
        "edge_ontime": "%.3f" % (so.get("mean") or 0.0),
        "edge_late": "%.3f" % (sl.get("mean") or 0.0),
        "lookahead_bias": "%.3f" % ((sl.get("mean") or 0.0) - (so.get("mean") or 0.0)),
        "updated": now,
    }
    con.executemany("INSERT OR REPLACE INTO blogger_score_meta(k,v,updated)"
                    " VALUES (?,?,?)", [(k, v, now) for k, v in meta.items()])
    con.commit()

    if show:
        print("=== 博主战绩（日度超额，已减同期全市场 T+5 基线）===")
        print("  %-18s %5s %5s %7s %8s %8s %6s %5s %s"
              % ("博主", "条数", "天数", "日均只数", "日均超额", "收缩超额", "t值", "评级", "稳定性"))
        for u in sorted(out, key=lambda x: -x[6]):
            print("  %-18s %5d %5d %7.1f %+8.2f %+8.2f %6.2f %5s %s"
                  % (u[0], u[1], u[2], u[3], u[4], u[6], u[7], u[12], u[11]))
        print("\n  基线口径：%d 个交易日自算，样本期 %s ~ %s"
              % (len(base), meta["period_from"], meta["period_to"]))
        print("  有效独立窗口 = 交易日数/fwd = %.1f （不足 20 时结论仅作方向参考）"
              % float(meta["eff_windows"]))
        print("  未匹配到基线的样本：%d 条（推荐日超出行情覆盖）" % skipped)
        print("  可执行性：交易日发布 %s 条 平均超额 %s ｜ 非交易日发布（基点在上个交易日，"
              "实际买不到）%s 条 平均超额 %s → 前视偏差 %s pt"
              % (meta["n_ontime"], meta["edge_ontime"], meta["n_late"],
                 meta["edge_late"], meta["lookahead_bias"]))

    return {"ok": True, "bloggers": len(out), "rows": len(rows), "skipped": skipped,
            "baseline_days": len(base), "secs": round(time.time() - t0, 2),
            "eff_windows": meta["eff_windows"]}


def _median(v):
    s = sorted(v)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def load(con: sqlite3.Connection | None = None) -> dict:
    """给接口/前端用：返回榜单 + 口径元数据。"""
    own = con is None
    if own:
        con = _conn()
    try:
        rows = con.execute("SELECT * FROM blogger_score"
                           " ORDER BY edge_shrunk DESC").fetchall()
        meta = {r["k"]: r["v"] for r in con.execute(
            "SELECT k, v FROM blogger_score_meta")}
        return {"ok": bool(rows), "list": [dict(r) for r in rows], "meta": meta}
    finally:
        if own:
            con.close()


def run() -> dict:
    con = _conn()
    try:
        ensure_tables(con)
        return compute(con)
    finally:
        con.close()


if __name__ == "__main__":
    import sys
    con = _conn()
    try:
        ensure_tables(con)
        r = compute(con, show="--show" in sys.argv)
    finally:
        con.close()
    print("[blogger_score]", r)
