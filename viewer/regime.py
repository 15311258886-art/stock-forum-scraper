# -*- coding: utf-8 -*-
"""市场状态分层（2026-10-29）。

为什么必须做这件事
------------------
「信号战绩」上线后，全样本口径给出的结论是：T+5 胜率 46.1%，扣费后 45.4%，
几乎没有一个信号能稳定跑赢基线。这个结论**本身是可疑的**：

    把牛市、熊市、震荡市的样本混在一起求平均，
    等于把「择时」和「选股」两种能力搅成一团泥，最后两边都看不见。
    熊市里全市场都在跌，再好的选股信号平均收益也是负的。

真正该问的是：**在同一个市场状态里，这个信号选出来的股票，比当天全市场
平均水平更好还是更差？** 以及它在哪几种状态下都成立（regime-robust），
还是只在某一种状态下有效（择时依赖）。

市场状态怎么定义（以及踩过的坑）
--------------------------------
口径：**全市场等权指数的 20 日累计收益** —— 即每日全市场个股收益的**均值**
滚动 20 日复利累乘。

  坑1（已修）：第一版用「中位数累乘」。中位数按定义必然低于均值（收益分布右偏），
  累乘后负偏被逐日放大，实测均值 -4.60%、中位数 -3.76%，直接导致 268 天里
  153 天下行、上行只有 18 天 —— 三档样本严重失衡，上行档根本不够下结论。
  等权指数的日收益就是**均值**，这才是"全市场涨了多少"的正解。

  坑2（已修）：第一版用绝对阈值 ±3%。样本期（2025-08~2026-10）全市场等权
  20 日收益 90 分位才 +2.11%，±3% 这种"看起来中性"的阈值在这个区间里等同于
  "只有极少数天才算上行"。改用**滚动分位**切档：以截至当日（不含当日）过去
  250 个交易日的 33/67 分位为界 —— 三档样本自动均衡，且完全没有前视。

三档的含义（务必按这个理解，不要说成"牛市/熊市"）
------------------------------------------------
  up    当前 20 日等权市场收益 > 过去一年 67 分位 ≈ 相对强势
  range 处于 33~67 分位之间            ≈ 相对中性
  down  低于过去一年 33 分位            ≈ 相对弱势

它是**相对过去一年**的强弱，不是绝对涨跌。所以 `up` 也可能是「跌得比别人慢」。
绝对方向单独用 `ret20` 的符号表达，展示层要同时给出，避免误读。

前视偏差
--------
`regime[d]` 只用 d 日及以前的数据（20 日回看 + 截至 d-1 的 250 日分位），不含
未来信息。它与信号入场口径一致：d 日收盘同时知道信号与状态，fwd 日后收盘结算。
因此本层不引入**额外**前视偏差（原回测「收盘买收盘卖」的假设不变）。

用法：
    .venv/Scripts/python.exe -m viewer.regime            # 重算并落库
    .venv/Scripts/python.exe -m viewer.regime --show     # 只打印，不落库
"""
import argparse
import os
import sqlite3
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")

WINDOW = 20          # 回看交易日（约一个月）
HIST = 250           # 分位阈值的回看窗口（约一年）
MIN_HIST = 60        # 分位窗口最少样本；不足时退回绝对阈值兜底
UP_TH = 0.03         # 兜底阈值（仅在样本不足的前几十天使用）
DOWN_TH = -0.03
Q_LO, Q_HI = 33.0, 67.0
REGIMES = ("up", "range", "down")
REGIME_CN = {"up": "强势", "range": "震荡", "down": "弱势"}
MAX_DAILY = 0.22     # 单日收益超过 ±22% 视为复权断层/数据错，剔除


def ensure_tables(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS market_regime (
        trade_date TEXT PRIMARY KEY,
        regime     TEXT NOT NULL,
        ret20      REAL,
        r_med      REAL,
        breadth    REAL,
        n_stocks   INTEGER,
        updated    TEXT)""")
    # 2026-10-29：口径修正后新增列（老库平滑升级，不重建）
    for col, decl in (("r_mean", "REAL"), ("q33", "REAL"), ("q67", "REAL")):
        try:
            cols = [d[1] for d in con.execute("PRAGMA table_info(market_regime)").fetchall()]
            if col not in cols:
                con.execute("ALTER TABLE market_regime ADD COLUMN %s %s" % (col, decl))
        except sqlite3.Error:
            pass
    con.execute("""CREATE TABLE IF NOT EXISTS signal_regime (
        reason      TEXT NOT NULL,
        direction   TEXT NOT NULL,
        fwd         INTEGER NOT NULL,
        regime      TEXT NOT NULL,
        n           INTEGER,
        winrate     REAL,
        avg_ret     REAL,
        sharpe      REAL,
        winrate_net REAL,
        avg_ret_net REAL,
        sharpe_net  REAL,
        edge_wr     REAL,
        edge_ret    REAL,
        fee         REAL,
        updated     TEXT,
        PRIMARY KEY (reason, direction, fwd, regime))""")
    con.execute("""CREATE TABLE IF NOT EXISTS signal_baseline_regime (
        fwd         INTEGER NOT NULL,
        regime      TEXT NOT NULL,
        n           INTEGER,
        winrate     REAL,
        avg_ret     REAL,
        winrate_net REAL,
        avg_ret_net REAL,
        fee         REAL,
        updated     TEXT,
        calc_version TEXT,
        lookback_days INTEGER,
        PRIMARY KEY (fwd, regime))""")
    # 2026-10-31：老库补列。分状态基线是「市况过滤」这一整套结论的地基，
    # 必须有版本戳 —— 否则口径一变，新旧两批数字会同时躺在这张表里且无法区分。
    # lookback_days 同理：250 日与 1250 日的分状态结论完全相反，只靠 calc_version 分不出。
    for _col, _decl in (("calc_version", "TEXT"), ("lookback_days", "INTEGER")):
        try:
            _bc = [d[1] for d in con.execute("PRAGMA table_info(signal_baseline_regime)").fetchall()]
            if _col not in _bc:
                con.execute("ALTER TABLE signal_baseline_regime ADD COLUMN %s %s" % (_col, _decl))
        except sqlite3.Error:
            pass
    con.commit()


def classify(ret20, lo, hi) -> str:
    if ret20 is None or (isinstance(ret20, float) and np.isnan(ret20)):
        return "range"
    if ret20 > hi:
        return "up"
    if ret20 < lo:
        return "down"
    return "range"


def compute(con: sqlite3.Connection):
    """返回 DataFrame：trade_date / r_mean / r_med / breadth / n_stocks / ret20 / q33 / q67 / regime。

    纯历史计算，不落库。调用方决定是否写回。
    """
    import pandas as pd

    t0 = time.time()
    df = pd.read_sql(
        "SELECT date AS trade_date, code, close FROM stock_indicators_daily "
        "WHERE close IS NOT NULL AND close > 0", con)
    if df.empty:
        return pd.DataFrame(columns=["trade_date", "r_mean", "r_med", "breadth",
                                     "n_stocks", "ret20", "q33", "q67", "regime"])
    df = df.sort_values(["code", "trade_date"], kind="mergesort")

    # 手工算日收益（不用 pct_change：各 pandas 版本 fill_method 语义不同，容易静默填坑）
    prev = df.groupby("code", sort=False)["close"].shift(1)
    df["r"] = df["close"] / prev - 1.0
    df.loc[df["r"].abs() > MAX_DAILY, "r"] = np.nan

    df["up"] = np.where(df["r"].notna(), (df["r"] > 0).astype(float), np.nan)
    daily = df.groupby("trade_date", sort=True).agg(
        r_mean=("r", "mean"),
        r_med=("r", "median"),
        breadth=("up", "mean"),
        n_stocks=("r", "count"),
    ).reset_index()

    # 等权指数：日均收益复利累乘 → 20 日累计
    r = daily["r_mean"].fillna(0.0).to_numpy(dtype=float)
    cum = np.cumprod(1.0 + r)
    n = len(r)
    ret20 = np.full(n, np.nan)
    for i in range(WINDOW, n):
        ret20[i] = cum[i] / cum[i - WINDOW] - 1.0

    # 滚动分位切档（不含当日，避免自指）
    q33s = np.full(n, np.nan)
    q67s = np.full(n, np.nan)
    regs = []
    for i in range(n):
        hist = ret20[max(0, i - HIST):i]
        hist = hist[~np.isnan(hist)]
        if len(hist) >= MIN_HIST:
            lo, hi = float(np.percentile(hist, Q_LO)), float(np.percentile(hist, Q_HI))
        else:
            lo, hi = DOWN_TH, UP_TH          # 前几十天样本不足，绝对阈值兜底
        q33s[i], q67s[i] = lo, hi
        regs.append(classify(ret20[i], lo, hi))

    daily["ret20"] = ret20
    daily["q33"] = q33s
    daily["q67"] = q67s
    daily["regime"] = regs
    print("[regime] %d 个交易日（%s ~ %s），耗时 %.1fs"
          % (len(daily), daily["trade_date"].iloc[0], daily["trade_date"].iloc[-1],
             time.time() - t0), flush=True)
    valid = daily[~daily["ret20"].isna()]
    if not valid.empty:
        print("[regime] 20日等权收益 均值%+.2f%% 中位%+.2f%% ｜ 分档 %s"
              % (valid["ret20"].mean() * 100, valid["ret20"].median() * 100,
                 dict(valid["regime"].value_counts())))
    return daily


def save(con: sqlite3.Connection, daily) -> int:
    ensure_tables(con)
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    def _r(x, nd=6):
        return None if (x is None or (isinstance(x, float) and np.isnan(x))) else round(float(x), nd)

    rows = []
    for _, r in daily.iterrows():
        if _r(r["ret20"]) is None:
            continue
        rows.append((str(r["trade_date"]), str(r["regime"]), _r(r["ret20"], 5),
                     _r(r["r_med"]), _r(r["breadth"], 4),
                     int(r["n_stocks"]) if not np.isnan(r["n_stocks"]) else 0, now,
                     _r(r["r_mean"]), _r(r["q33"], 5), _r(r["q67"], 5)))
    if rows:
        # 先清空再写：口径变更后重算时，若只依赖 REPLACE，上一版多出来的日期会残留成
        # 脏数据（例如旧口径下前 20 天也有值、新口径下被跳过），两个口径混在一起最危险。
        con.execute("DELETE FROM market_regime")
        con.executemany(
            "INSERT OR REPLACE INTO market_regime"
            "(trade_date,regime,ret20,r_med,breadth,n_stocks,updated,r_mean,q33,q67)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    return len(rows)


def load_map(con: sqlite3.Connection) -> dict:
    """{trade_date: regime}。回测循环里逐日查，dict 比 SQL 快两个数量级。"""
    try:
        return {r[0]: r[1] for r in con.execute("SELECT trade_date, regime FROM market_regime")}
    except sqlite3.Error:
        return {}


def load_baseline(con: sqlite3.Connection) -> dict:
    """{(fwd, regime): {...}} 供 evidence / 接口直接消费。"""
    out = {}
    try:
        for fwd, reg, n, wr, avg, wrn, avgn in con.execute(
                "SELECT fwd,regime,n,winrate,avg_ret,winrate_net,avg_ret_net "
                "FROM signal_baseline_regime"):
            out[(int(fwd), reg)] = {"n": n, "winrate": wr, "avg_ret": avg,
                                    "winrate_net": wrn, "avg_ret_net": avgn}
    except sqlite3.Error:
        pass
    return out


def current(con: sqlite3.Connection) -> dict:
    """最新交易日的状态 + 三档分布（某档样本太少时，该档结论不可信，展示层要提示）。"""
    try:
        row = con.execute(
            "SELECT trade_date,regime,ret20,breadth,n_stocks,q33,q67 FROM market_regime "
            "ORDER BY trade_date DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    dist = {}
    try:
        for reg, cnt in con.execute("SELECT regime, COUNT(*) FROM market_regime GROUP BY regime"):
            dist[reg] = cnt
    except sqlite3.Error:
        pass
    return {"date": row[0], "regime": row[1], "regime_cn": REGIME_CN.get(row[1], row[1]),
            "ret20": row[2], "breadth": row[3], "n_stocks": row[4],
            "q33": row[5], "q67": row[6], "dist": dist,
            "window": WINDOW, "hist": HIST, "q_lo": Q_LO, "q_hi": Q_HI,
            "note": "分档为相对过去%d个交易日的强弱（33/67分位），非绝对涨跌" % HIST}


def run(save_it: bool = True) -> dict:
    con = sqlite3.connect(DB, timeout=30)
    try:
        daily = compute(con)
        n = save(con, daily) if save_it else 0
        if not daily.empty:
            for _, r in daily.tail(8).iterrows():
                if isinstance(r["ret20"], (int, float)) and not np.isnan(r["ret20"]):
                    print("  %s  %-5s  20日%+6.2f%%  上涨占比%.0f%%  分位界[%+.1f%%, %+.1f%%]"
                          % (r["trade_date"], REGIME_CN[r["regime"]], r["ret20"] * 100,
                             (r["breadth"] or 0) * 100,
                             (r["q33"] or 0) * 100, (r["q67"] or 0) * 100))
        cur = current(con)
        if cur:
            print("[regime] 最新 %s = %s（20日 %+.2f%%）｜历史分布 %s"
                  % (cur["date"], cur["regime_cn"], (cur["ret20"] or 0) * 100, cur["dist"]))
        return {"ok": True, "saved": n, "current": cur}
    finally:
        con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="只打印不落库")
    a = ap.parse_args()
    run(save_it=not a.show)
