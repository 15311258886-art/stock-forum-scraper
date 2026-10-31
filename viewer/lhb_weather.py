# -*- coding: utf-8 -*-
"""viewer/lhb_weather.py —— 龙虎榜「资金风向」指标（2026-10-30 D35）

## 为什么要有这个模块

产品定位变更：**龙虎榜从「选股依据」降级为「市场风向指标」**。依据是本项目自己的数据
（`viewer/avoid.py`，4077 个交易日 / 37.3 万样本）：

| 口径 | T+5 超额 | t | 结论 |
|---|---|---|---|
| 近10日上过龙虎榜（不限换手） | +0.006% | 0.07 | 纯噪声 |
| 上榜且换手 < 15% | +0.881% | +10.4 | 显著跑赢 |
| 上榜且换手 ≥ 15% | −0.91% | −11.28 | 显著跑输（胜率 39.9%） |

也就是说「上榜」本身没有信息量，有分辨力的是换手率。龙虎榜的正确用途是回答
「今天钱在哪儿折腾得多」，不是「哪只票能买」。

## 四个维度，只有两个能进合成（数据现实决定的）

    lhb_stocks        2010-01-04 ~ 2026-10-29   4083 个交易日   d1_chg 覆盖 99.2%
    lhb_inst          2026-09-16 ~ 2026-10-29     26 个交易日
    lhb_depts         2026-09-16 ~ 2026-10-29     26 个交易日
    lhb_sector_focus  2026-09-16 ~ 2026-10-28     25 个交易日

    ┌──────────────┬──────────┬──────────────────────────────────────┐
    │ 维度         │ 样本     │ 处置                                 │
    ├──────────────┼──────────┼──────────────────────────────────────┤
    │ 参与度       │ 16 年    │ 250 日滚动分位 → **进合成分**        │
    │ 赚钱效应     │ 16 年    │ 250 日滚动分位 → **进合成分**        │
    │ 结构(机构/游资)│ 26 天  │ 只存值 + sample_days=26，不定档      │
    │ 集中度(板块) │ 25 天    │ 只存值 + sample_days=25，不定档      │
    └──────────────┴──────────┴──────────────────────────────────────┘

短样本不得参与判定，与 D25「短样本不得重仓」一致。`MIN_SAMPLE_FOR_STATE = 250` 是升级开关：
结构/集中度的样本天数到齐后，把 `state_dims` 从 ("participation","money_effect") 扩进去即可，
**不要**现在就把 26 天的数据混进档位。

## 两个容易做错的地方

1. **赚钱效应必须 lag 1 个交易日**：`lhb_stocks.d1_chg` 是「上榜次日涨跌幅」，算 d 日的赚钱
   效应只能用 `trade_date <= d-1` 的样本。实测 2026-10-29 当天 `d1_chg` 全为 NULL —— 那是
   还没发生的收益，直接用就是典型的前视偏差。
2. **`net_amt_ratio` 的分母在 app.db 里不存在**：`stock_indicators_daily` 只有 close（无
   amount/volume），`stock_daily_snapshot.amount` 只有 2026-10-27 起的 2 天。所以全市场成交额
   改用 `data/kline_daily.db` 的 `SUM(v*c)`（v 是**股**，实测 2026-10-29 得 1.5366e12 元，
   与 `stock_daily_snapshot.amount` 的 1.5580e12 相差 1.4%，可用）。该分母只影响
   `net_amt_ratio` 一个指标，且分子分母同源同量纲。

## 用法

    python -m viewer.lhb_weather --backtest      # 只算 + 回测，不落库
    python -m viewer.lhb_weather --save          # 算 + 写 lhb_weather_daily
    python -m viewer.lhb_weather --backtest --save
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("VIEWER_DB") or os.path.join(BASE, "viewer", "app.db")
KLINE_DB = os.path.join(BASE, "data", "kline_daily.db")
TABLE = "lhb_weather_daily"

HIST = 250                # 滚动分位窗口（交易日）
MIN_HIST = 60             # 分位至少要这么多样本，否则留空（不猜）
Q_LO, Q_HI = 33.0, 67.0   # 与 viewer/regime.py 的 Q_LO/Q_HI 同口径
D1_WINDOW = 20            # 赚钱效应回看多少个「上榜日」
MIN_SAMPLE_FOR_STATE = 250   # 结构/集中度升级为「参与定档」的最小样本（当前 26/25 天，不够）
FALLBACK_DENOM = 5000     # 分母兜底（失真，必须标 denom_src='fallback'）
IC_WINDOW = 60            # 滚动 IC 窗口
SEGMENTS = (("2010-2015", "2010-01-01", "2015-12-31"),
            ("2016-2020", "2016-01-01", "2020-12-31"),
            ("2021-2026", "2021-01-01", "2026-12-31"))

DDL = """
CREATE TABLE IF NOT EXISTS lhb_weather_daily (
    trade_date            TEXT PRIMARY KEY,
    n_listed              INTEGER,
    denom                 INTEGER,
    denom_src             TEXT,
    list_ratio            REAL,
    net_amt               REAL,
    market_amount         REAL,
    net_amt_ratio         REAL,
    part_pct              REAL,
    d1_mean_20            REAL,
    d1_win_20             REAL,
    effect_pct            REAL,
    score                 REAL,
    q33                   REAL,
    q67                   REAL,
    state                 TEXT,
    inst_net              REAL,
    hot_net               REAL,
    inst_share            REAL,
    top3_share            REAL,
    hhi                   REAL,
    sample_days_structure INTEGER,
    sample_days_sector    INTEGER,
    updated_at            TEXT
)
"""


# ---------------------------------------------------------------- 基础工具
def _ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _rw(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


def _f(x):
    """NaN/None → None（落库用）。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v or v in (float("inf"), float("-inf")) else v


# ---------------------------------------------------------------- 分母 / 全市场成交额
def denominator_by_date(con: sqlite3.Connection) -> dict:
    """每日「有成交的股票数」——`list_ratio` 的分母。

    优先级（lead 指定）：`stock_indicators_daily` 按 date 计数 → `stock_daily_snapshot`
    按 trade_date 计数 → 5000 兜底。返回 {date: (denom, src)}，src ∈ {indicators, snapshot, fallback}。
    """
    out: dict = {}
    try:
        for d, n in con.execute(
                "SELECT date, COUNT(*) FROM stock_indicators_daily "
                "WHERE close IS NOT NULL AND close > 0 GROUP BY date"):
            out[str(d)] = (int(n), "indicators")
    except Exception as e:  # noqa: BLE001
        print("[lhb_weather] 分母主源不可用（%s），降级 snapshot" % e, flush=True)
    try:
        for d, n in con.execute(
                "SELECT trade_date, COUNT(*) FROM stock_daily_snapshot "
                "WHERE close IS NOT NULL AND close > 0 GROUP BY trade_date"):
            out[str(d)] = (int(n), "snapshot")      # 覆盖更近的日期（若主源缺当日）
    except Exception:  # noqa: BLE001
        pass
    return out


def market_daily(kline_db: str = KLINE_DB, start: int = 20100101) -> pd.DataFrame:
    """全市场逐日：成交额 `SUM(v*c)` 与**等权平均日收益**（一次扫描算两样）。

    - `v` 是**股**（实测 2026-10-29 Σv·c = 1.5366e12 元 vs snapshot amount 1.5580e12，差 1.4%）；
    - 等权日收益 = 每只票 `c_t/c_{t-1} - 1` 的横截面均值（剔除前一日无价的票）；
    - 按 (code, d) 索引顺序流式读，避免 1378 万行一次性进内存。
    """
    con = _ro(kline_db)
    amt: dict = {}
    rsum: dict = {}
    rn: dict = {}
    prev_code = None
    prev_c = None
    cur = con.execute(
        "SELECT code, d, c, v FROM kline WHERE d>=? AND c>0 ORDER BY code, d", (start,))
    while True:
        rows = cur.fetchmany(200000)
        if not rows:
            break
        for code, d, c, v in rows:
            amt[d] = amt.get(d, 0.0) + (float(v or 0.0) * float(c))
            if code == prev_code and prev_c:
                rsum[d] = rsum.get(d, 0.0) + (float(c) / prev_c - 1.0)
                rn[d] = rn.get(d, 0) + 1
            prev_code, prev_c = code, float(c)
    con.close()
    ds = sorted(amt)
    recs = []
    for d in ds:
        s = str(d)
        recs.append({
            "trade_date": "%s-%s-%s" % (s[:4], s[4:6], s[6:8]),
            "market_amount": amt.get(d, np.nan),
            "r_mean": (rsum[d] / rn[d]) if rn.get(d) else np.nan,
            "n_ret": rn.get(d, 0),
        })
    return pd.DataFrame(recs)


# ---------------------------------------------------------------- 滚动分位 / 分档
def roll_pct(s: pd.Series, hist: int = HIST, min_hist: int = MIN_HIST) -> pd.Series:
    """当前值在过去 `hist` 个交易日（**不含当日**）中的分位（0~100）。

    与 `viewer/regime.py` 的口径一致：阈值/分布都只用当日**之前**的样本，杜绝自指。
    样本不足 `min_hist` → NaN（不猜）。
    """
    v = s.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        lo = max(0, i - hist)
        h = v[lo:i]
        h = h[~np.isnan(h)]
        if len(h) < min_hist:
            continue
        cur = v[i]
        if np.isnan(cur):
            continue
        out[i] = float((h < cur).sum()) * 100.0 / len(h)
    return pd.Series(out, index=s.index)


def roll_thresholds(s: pd.Series, hist: int = HIST, min_hist: int = MIN_HIST,
                    q_lo: float = Q_LO, q_hi: float = Q_HI):
    """滚动 q_lo/q_hi 分位阈值（样本不含当日）。返回 (lo_series, hi_series)。"""
    v = s.to_numpy(dtype=float)
    lo_out = np.full(len(v), np.nan)
    hi_out = np.full(len(v), np.nan)
    for i in range(len(v)):
        lo = max(0, i - hist)
        h = v[lo:i]
        h = h[~np.isnan(h)]
        if len(h) < min_hist:
            continue
        lo_out[i] = float(np.percentile(h, q_lo))
        hi_out[i] = float(np.percentile(h, q_hi))
    return pd.Series(lo_out, index=s.index), pd.Series(hi_out, index=s.index)


def classify(score, lo, hi) -> str:
    """照抄 `viewer/regime.py::classify` 的形状（阈值含端点外侧）。

    score 缺失 → `"unknown"`；阈值样本不足 → `"unknown"`（**不**用绝对阈值硬套，
    因为 score 是 0~100 的分位合成，绝对阈值随市场结构漂移）。
    """
    if score is None or (isinstance(score, float) and np.isnan(score)):
        return "unknown"
    if lo is None or hi is None or (isinstance(lo, float) and np.isnan(lo)) \
            or (isinstance(hi, float) and np.isnan(hi)):
        return "unknown"
    if score > hi:
        return "hot"
    if score < lo:
        return "cold"
    return "neutral"


# ---------------------------------------------------------------- 主计算
def compute(con: sqlite3.Connection | None = None, verbose: bool = True,
            market: pd.DataFrame | None = None) -> pd.DataFrame:
    """算四个维度 + 合成分 + 档位，返回逐日 DataFrame（不落库）。

    `market` 是**测试注入口**（默认 None = 去扫 `data/kline_daily.db` 的 1382 万行）。
    单测必须跑在 tmp 库上、且不能每条用例都花 4 分钟扫生产日线库，所以允许外部传入
    同形状的 DataFrame（列：trade_date / market_amount / r_mean）。生产调用方一律不传。
    """
    t0 = time.time()
    own = con is None
    if own:
        con = _ro(DB)
    try:
        lhb = pd.read_sql(
            "SELECT trade_date, security_code, net_amt, d1_chg FROM lhb_stocks", con)
        inst = pd.read_sql(
            "SELECT trade_date, security_code, net_buy_amt FROM lhb_inst", con)
        depts = pd.read_sql(
            "SELECT trade_date, operate_name, net_amt FROM lhb_depts", con)
        sector = pd.read_sql(
            "SELECT trade_date, sector, net_amt FROM lhb_sector_focus", con)
        denoms = denominator_by_date(con)
    finally:
        if own:
            con.close()

    if lhb.empty:
        raise RuntimeError("lhb_stocks 为空，无法计算资金风向")

    lhb["trade_date"] = lhb["trade_date"].astype(str)
    daily = lhb.groupby("trade_date", sort=True).agg(
        n_listed=("security_code", "nunique"),
        net_amt=("net_amt", "sum"),
        d1_n=("d1_chg", "count"),
        d1_sum=("d1_chg", "sum"),
        d1_win=("d1_chg", lambda s: (s > 0).sum()),
    ).reset_index()

    # ── 参与度 ────────────────────────────────────────────────
    daily["denom"] = [denoms.get(d, (FALLBACK_DENOM, "fallback"))[0] for d in daily["trade_date"]]
    daily["denom_src"] = [denoms.get(d, (FALLBACK_DENOM, "fallback"))[1] for d in daily["trade_date"]]
    daily["list_ratio"] = daily["n_listed"] / daily["denom"].replace(0, np.nan)

    mkt = market if market is not None else market_daily()
    daily = daily.merge(mkt, on="trade_date", how="left")
    daily["net_amt_ratio"] = daily["net_amt"] / daily["market_amount"].replace(0, np.nan)

    daily["part_pct_a"] = roll_pct(daily["list_ratio"])
    daily["part_pct_b"] = roll_pct(daily["net_amt_ratio"])
    daily["part_pct"] = (daily["part_pct_a"] + daily["part_pct_b"]) / 2.0

    # ── 赚钱效应（lag 1 个交易日，防前视）────────────────────
    # 每个上榜日的 d1 统计只对「之后的日期」可用：先算出逐日统计，再 shift(1)。
    daily = daily.sort_values("trade_date").reset_index(drop=True)
    lag_n = daily["d1_n"].shift(1).rolling(D1_WINDOW, min_periods=1).sum()
    lag_sum = daily["d1_sum"].shift(1).rolling(D1_WINDOW, min_periods=1).sum()
    lag_win = daily["d1_win"].shift(1).rolling(D1_WINDOW, min_periods=1).sum()
    daily["d1_mean_20"] = np.where(lag_n > 0, lag_sum / lag_n.replace(0, np.nan), np.nan)
    daily["d1_win_20"] = np.where(lag_n > 0, lag_win / lag_n.replace(0, np.nan), np.nan)
    daily["effect_pct"] = roll_pct(daily["d1_mean_20"])

    # ── 合成分与档位（先等权，别拍脑袋调权）──────────────────
    daily["score"] = 0.5 * daily["part_pct"] + 0.5 * daily["effect_pct"]
    lo, hi = roll_thresholds(daily["score"])
    daily["q33"] = lo
    daily["q67"] = hi
    daily["state"] = [classify(s, a, b) for s, a, b in
                      zip(daily["score"], daily["q33"], daily["q67"])]

    # ── 结构（26 天，只展示）──────────────────────────────────
    if not inst.empty:
        inst["trade_date"] = inst["trade_date"].astype(str)
        i_agg = inst.groupby("trade_date")["net_buy_amt"].sum().rename("inst_net")
    else:
        i_agg = pd.Series(dtype=float, name="inst_net")
    if not depts.empty:
        depts["trade_date"] = depts["trade_date"].astype(str)
        hot = depts[~depts["operate_name"].fillna("").str.contains("机构专用")]
        h_agg = hot.groupby("trade_date")["net_amt"].sum().rename("hot_net")
    else:
        h_agg = pd.Series(dtype=float, name="hot_net")
    daily = daily.merge(i_agg, left_on="trade_date", right_index=True, how="left")
    daily = daily.merge(h_agg, left_on="trade_date", right_index=True, how="left")
    den = daily["inst_net"].abs() + daily["hot_net"].abs()
    daily["inst_share"] = np.where(den > 0, daily["inst_net"] / den.replace(0, np.nan), np.nan)
    daily["sample_days_structure"] = int(inst["trade_date"].nunique()) if not inst.empty else 0

    # ── 集中度（25 天，只展示）────────────────────────────────
    if not sector.empty:
        sector["trade_date"] = sector["trade_date"].astype(str)
        s = sector.copy()
        s["absnet"] = s["net_amt"].abs()
        tot_by_day = s.groupby("trade_date")["absnet"].sum()
        top3 = (s.sort_values(["trade_date", "absnet"], ascending=[True, False])
                 .groupby("trade_date")["absnet"].apply(lambda x: float(x.head(3).sum())))
        s["share"] = s["net_amt"] / tot_by_day.reindex(s["trade_date"]).to_numpy()
        hhi = (s["share"].fillna(0.0) ** 2).groupby(s["trade_date"]).sum()
        agg = pd.DataFrame({
            "top3_share": top3 / tot_by_day.reindex(top3.index).replace(0, np.nan),
            "hhi": hhi,
        })
        daily = daily.merge(agg, left_on="trade_date", right_index=True, how="left")
    else:
        daily["top3_share"] = np.nan
        daily["hhi"] = np.nan
    daily["sample_days_sector"] = int(sector["trade_date"].nunique()) if not sector.empty else 0

    daily["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if verbose:
        v = daily[daily["score"].notna()]
        # 2026-10-30 D35 阶段二订正：原来这行把 `score` 非空叫「可定档」，多算了 60 天。
        #   `roll_thresholds` 需要 MIN_HIST=60 个历史观测，最早 60 个有分值的日期阈值仍是 NaN、
        #   classify 返回 unknown —— 那是**正确行为**，只是标签叫错了名。
        n_state = int(daily["state"].isin(["hot", "neutral", "cold"]).sum())
        print("[lhb_weather] %d 个上榜日（%s ~ %s），有分值 %d 天 / 可定档 %d 天，耗时 %.1fs"
              % (len(daily), daily["trade_date"].iloc[0], daily["trade_date"].iloc[-1],
                 len(v), n_state, time.time() - t0), flush=True)
    return daily


# ---------------------------------------------------------------- 落库
COLS = ["trade_date", "n_listed", "denom", "denom_src", "list_ratio", "net_amt",
        "market_amount", "net_amt_ratio", "part_pct", "d1_mean_20", "d1_win_20",
        "effect_pct", "score", "q33", "q67", "state", "inst_net", "hot_net",
        "inst_share", "top3_share", "hhi", "sample_days_structure",
        "sample_days_sector", "updated_at"]


def save(con: sqlite3.Connection, daily: pd.DataFrame) -> int:
    """整表重写自己那张 `lhb_weather_daily`（**绝不碰 market_regime**）。

    先 DELETE 再写，理由与 regime.save 相同：口径变更后若只靠 REPLACE，上一版多出来的
    日期会残留成脏数据，两个口径混在同一张表里最危险。
    """
    ensure_schema(con)
    rows = []
    for _, r in daily.iterrows():
        rows.append((
            str(r["trade_date"]), int(r["n_listed"]), int(r["denom"]), str(r["denom_src"]),
            _f(r["list_ratio"]), _f(r["net_amt"]), _f(r["market_amount"]),
            _f(r["net_amt_ratio"]), _f(r["part_pct"]), _f(r["d1_mean_20"]),
            _f(r["d1_win_20"]), _f(r["effect_pct"]), _f(r["score"]), _f(r["q33"]),
            _f(r["q67"]), str(r["state"]), _f(r.get("inst_net")), _f(r.get("hot_net")),
            _f(r.get("inst_share")), _f(r.get("top3_share")), _f(r.get("hhi")),
            int(r.get("sample_days_structure") or 0), int(r.get("sample_days_sector") or 0),
            str(r["updated_at"]),
        ))
    con.execute("DELETE FROM %s" % TABLE)
    con.executemany("INSERT INTO %s(%s) VALUES(%s)"
                    % (TABLE, ",".join(COLS), ",".join("?" * len(COLS))), rows)
    con.commit()
    return len(rows)


# ---------------------------------------------------------------- 回测
def _tstat(a, b) -> float:
    """Welch t（两组均值差）。样本不足 → nan。"""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a = a[~np.isnan(a)]
    b = b[~np.isnan(b)]
    if len(a) < 3 or len(b) < 3:
        return float("nan")
    va, vb = a.var(ddof=1), b.var(ddof=1)
    se = np.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return float("nan")
    return float((a.mean() - b.mean()) / se)


def _spearman(x: pd.Series, y: pd.Series) -> float:
    """Spearman = 秩上的 Pearson。**不用 pandas `method="spearman"`** —— 它会去 import
    scipy，而本环境的 venv 没有 scipy（实测 `ModuleNotFoundError`）。自己算一遍更稳。
    """
    m = x.notna() & y.notna()
    a = x[m]
    b = y[m]
    if len(a) < 10:
        return float("nan")
    ra = a.rank().to_numpy(dtype=float)
    rb = b.rank().to_numpy(dtype=float)
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def _roll_spearman(x: pd.Series, y: pd.Series, win: int = IC_WINDOW) -> np.ndarray:
    """滚动窗口 Spearman（窗口内先取秩再算 Pearson，等价且不用 scipy）。"""
    xv = x.to_numpy(dtype=float)
    yv = y.to_numpy(dtype=float)
    out = np.full(len(xv), np.nan)
    for i in range(win - 1, len(xv)):
        xs = xv[i - win + 1:i + 1]
        ys = yv[i - win + 1:i + 1]
        m = ~np.isnan(xs) & ~np.isnan(ys)
        if m.sum() < 10:
            continue
        rx = pd.Series(xs[m]).rank().to_numpy()
        ry = pd.Series(ys[m]).rank().to_numpy()
        if rx.std() == 0 or ry.std() == 0:
            continue
        out[i] = float(np.corrcoef(rx, ry)[0, 1])
    return out


def _bucket_state(v: pd.Series, lo: pd.Series, hi: pd.Series) -> pd.Series:
    return pd.Series([classify(a, b, c) for a, b, c in zip(v, lo, hi)], index=v.index)


def backtest(daily: pd.DataFrame) -> dict:
    """分档单调性 + IC + 子维度 + 三段稳健性。全部打印，同时返回 dict。"""
    df = daily.copy()
    # 前瞻收益：用全市场等权日收益连乘（-1 是"当日收盘"之后才可知，天然 t+1 起算）
    r = df["r_mean"].fillna(0.0).to_numpy(dtype=float)
    n = len(df)
    fw1 = np.full(n, np.nan)
    fw5 = np.full(n, np.nan)
    for i in range(n - 1):
        fw1[i] = r[i + 1]
        if i + 5 < n:
            fw5[i] = float(np.prod(1.0 + r[i + 1:i + 6]) - 1.0)
    df["fw1"] = fw1
    df["fw5"] = fw5

    res: dict = {}
    lab = df[df["state"].isin(["hot", "neutral", "cold"])]
    print("\n=== ① 分档单调性（全市场等权前瞻收益，仅可定档的 %d 天）===" % len(lab))
    print("  档位      N       T+1 均值    胜率     |      T+5 均值    胜率")
    for st in ("hot", "neutral", "cold"):
        a = lab[lab["state"] == st]["fw1"].dropna()
        b = lab[lab["state"] == st]["fw5"].dropna()
        print("  %-8s %5d   %+8.3f%%   %5.1f%%     |   %+8.3f%%   %5.1f%%"
              % (st, len(b), (a.mean() * 100 if len(a) else float("nan")),
                 (a > 0).mean() * 100 if len(a) else float("nan"),
                 (b.mean() * 100 if len(b) else float("nan")),
                 (b > 0).mean() * 100 if len(b) else float("nan")))
    hot5 = lab[lab["state"] == "hot"]["fw5"].dropna()
    cold5 = lab[lab["state"] == "cold"]["fw5"].dropna()
    hot1 = lab[lab["state"] == "hot"]["fw1"].dropna()
    cold1 = lab[lab["state"] == "cold"]["fw1"].dropna()
    t5 = _tstat(hot5, cold5)
    t1 = _tstat(hot1, cold1)
    print("  hot−cold  T+5 差 %+.3f%%  t=%+.2f   ｜ T+1 差 %+.3f%%  t=%+.2f"
          % ((hot5.mean() - cold5.mean()) * 100 if len(hot5) and len(cold5) else float("nan"),
             t5,
             (hot1.mean() - cold1.mean()) * 100 if len(hot1) and len(cold1) else float("nan"),
             t1))
    res["t5_hot_minus_cold"] = t5
    res["t1_hot_minus_cold"] = t1

    print("\n=== ② IC（score vs 次日/未来5日全市场等权收益；时间序列口径）===")
    for col in ("fw1", "fw5"):
        sub = df[["score", col]].dropna()
        ic_full = _spearman(sub["score"], sub[col]) if len(sub) > 10 else float("nan")
        rc = _roll_spearman(df["score"], df[col])
        rc = rc[~np.isnan(rc)]
        if len(rc) > 2:
            ir = rc.mean() / rc.std(ddof=1)
            t = rc.mean() / (rc.std(ddof=1) / np.sqrt(len(rc)))
        else:
            ir = t = float("nan")
        print("  vs %s : 全样本 Spearman IC=%+.4f（n=%d）｜ 滚动%d日 IC 均值%+.4f "
              "IR=%+.2f t=%+.2f（%d 窗）"
              % (col, ic_full, len(sub), IC_WINDOW, rc.mean() if len(rc) else float("nan"),
                 ir, t, len(rc)))
        res["ic_%s" % col] = ic_full
        res["ic_%s_t" % col] = t

    print("\n=== ③ 子维度各自的表现（各自的 250 日 33/67 分位分档）===")
    for name, col in (("参与度", "part_pct"), ("赚钱效应", "effect_pct")):
        lo, hi = roll_thresholds(df[col])
        st = _bucket_state(df[col], lo, hi)
        d2 = df.assign(state=st)
        line1, line5 = [], []
        for s in ("hot", "neutral", "cold"):
            a = d2[d2["state"] == s]["fw1"].dropna()
            b = d2[d2["state"] == s]["fw5"].dropna()
            line1.append("%s T+1 %+.3f%%(n=%d)" % (s, a.mean() * 100 if len(a) else float("nan"), len(a)))
            line5.append("%s T+5 %+.3f%%(n=%d)" % (s, b.mean() * 100 if len(b) else float("nan"), len(b)))
        sub = df[[col, "fw1"]].dropna()
        ic = _spearman(sub[col], sub["fw1"]) if len(sub) > 10 else float("nan")
        print("  【%s】IC(vs T+1)=%+.4f" % (name, ic))
        print("     " + " ｜ ".join(line1))
        print("     " + " ｜ ".join(line5))
        res["ic_%s" % col] = ic

    print("\n=== ④ 三段稳健性（标签沿用全样本口径，不重估）===")
    print("  区间          N      hot T+5     neutral T+5   cold T+5    hot−cold t")
    for tag, a, b in SEGMENTS:
        seg = lab[(lab["trade_date"] >= a) & (lab["trade_date"] <= b)]
        if len(seg) < 20:
            print("  %-12s %5d   （样本不足）" % (tag, len(seg)))
            continue
        h = seg[seg["state"] == "hot"]["fw5"].dropna()
        m = seg[seg["state"] == "neutral"]["fw5"].dropna()
        c = seg[seg["state"] == "cold"]["fw5"].dropna()
        print("  %-12s %5d   %+8.3f%%   %+8.3f%%   %+8.3f%%    %+.2f"
              % (tag, len(seg),
                 h.mean() * 100 if len(h) else float("nan"),
                 m.mean() * 100 if len(m) else float("nan"),
                 c.mean() * 100 if len(c) else float("nan"),
                 _tstat(h, c)))
        res["seg_%s_t" % tag] = _tstat(h, c)

    return res


def verdict(res: dict) -> str:
    """按**事先写死**的判定标准给结论，避免事后解释。"""
    t5 = res.get("t5_hot_minus_cold", float("nan"))
    t1 = res.get("t1_hot_minus_cold", float("nan"))
    if t5 == t5 and t5 > 2:
        return "通过：三档 T+5 单调且 hot−cold t>2，可进判定"
    if t1 == t1 and abs(t1) > 2:
        return "降级：仅短周期（T+1）有区分力、T+5 不显著 → 只做展示，不改判定"
    return "未通过：分档/IC 不显著 → 只做展示，不接判定"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="龙虎榜资金风向指标（D35）")
    p.add_argument("--backtest", action="store_true", help="跑分档/IC/稳健性回测")
    p.add_argument("--save", action="store_true", help="写入 lhb_weather_daily")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    daily = compute(verbose=not args.quiet)
    n_src = daily["denom_src"].value_counts().to_dict()
    print("[lhb_weather] 分母来源分布: %s" % n_src)
    print("[lhb_weather] 结构样本 %d 天、集中度样本 %d 天（MIN_SAMPLE_FOR_STATE=%d，"
          "均未达阈值 → 只展示不定档）"
          % (int(daily["sample_days_structure"].iloc[-1]),
             int(daily["sample_days_sector"].iloc[-1]), MIN_SAMPLE_FOR_STATE))

    res = {}
    if args.backtest:
        res = backtest(daily)
        print("\n=== 判定 ===")
        print("  " + verdict(res))

    if args.save:
        con = _rw(DB)
        try:
            n = save(con, daily)
            print("\n[lhb_weather] 已写入 %s：%d 行" % (TABLE, n))
        finally:
            con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
