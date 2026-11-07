# -*- coding: utf-8 -*-
"""条件化回测（第一条：箱体底部）—— 2026-11-05

## 为什么要有这个脚本

上一轮所有回测都是**绝对超额**：个股收益 − 同日全市场等权。那个指标回答的是
「这东西比随便买强吗」。但对「箱体底部」这类工具，问错了对象：

  · 它的职责不是跑赢全市场，是**在一批正在箱体里运行的票里，把位置低的那几只挑出来**；
  · 牛市里箱体票整体跑输，用绝对超额判它，它必输 —— 但它在池内的区分力可能很好。

所以本脚本改用**池内区分力**：
  前置池 P = 通过箱体一级+二级门槛的票（工具自己的适用条件）
  触发组   = P 里 pos ≤ 1/3（箱体下三分之一，工具定义的「谷底区」）
  对照组   = **P 里 pos > 1/3 的票**（同一天、同一池、同样的箱体状态）
  主指标   = 触发组 − 对照组（不是 − 全市场）

持有期按工具结构定：箱体是 3~6 个月的事，主看 T+60 / T+120；T+20 只作对照。

## 数据
用 `data/kline_daily.db`（前复权）。**不能用 kbase**：箱体判据要求 750 根日线，
kbase 每只票只有约 660 根，历史不够就会整天判不出箱体（假阴性）。
前复权在除权日无机械缺口（`tools/audit_hfq_gap.py` 已证），收益可直接用。

成本：0.25% 往返。**两组都扣，差值里自动抵消**，所以池内比较不受成本影响；
只有「vs 全市场」那一列需要看成本。

## 用法
  python tools/cond_box_backtest.py                 # 全窗口
  python tools/cond_box_backtest.py --since 2026-01-01   # 调试
产出：viewer/logs/cond_box_<stamp>.json + 控制台表格
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
QFQ = ROOT / "data" / "kline_daily.db"
APP = ROOT / "viewer" / "app.db"
OUT = ROOT / "viewer" / "logs"

# ---- 与 viewer/universe_box.py 完全一致的箱体门槛（改这里必须同步那边）----
MIN_BARS = 750
BOX_WIN = 750
MIN_AMOUNT_AVG = 3.0e7
PCT_MAX = 30.0
CV_MAX = 40.0
SLOPE_MAX = 18.0
TOUCH_MIN = 2
DWELL_BOTTOM_RATIO = 1.0 / 3.0
DWELL_LOOKBACK = 250
MIN_DWELL = 0.15          # 工具默认参数（_apply_box 的 min_dwell）

HOLDS = (20, 60, 120)     # 20 只作对照；60/120 是箱体的真实尺度
COST = 0.0025             # 往返成本（两组同扣，比较时可抵消）


def load_signal_dates(con: sqlite3.Connection, since: str, until: str) -> list[int]:
    """信号日 = 数据齐全的交易日。

    为什么用相对门槛：写死「当天票数≥3000」会随上市数量增长误杀早年 —— 2010 年
    全市场只有 1953 只，2016 年底才 2879 只，绝对门槛直接让 2017 年之前的样本全部
    消失（实测把 2012 起的窗口砍成了 2017 起）。改成「不低于前后 120 个交易日最大
    票数的 60%」，2010 和 2026 用同一把尺子，只剔掉真的缺数据的日子。

    坑：别名不能叫 c —— kline 表自带收盘价列 c，`HAVING c>=3000` 会被 SQLite
    优先解析成「收盘价≥3000元」，直接筛空。这里写成 COUNT(*) 彻底避开。
    """
    rows = con.execute(
        "SELECT d, COUNT(*) AS cnt FROM kline WHERE d BETWEEN ? AND ?"
        " GROUP BY d ORDER BY d",
        (int(since.replace("-", "")), int(until.replace("-", "")))).fetchall()
    if not rows:
        return []
    ds = [int(r[0]) for r in rows]
    cs = np.array([int(r[1]) for r in rows], dtype=np.float64)
    keep, n = [], len(cs)
    for i in range(n):
        lo, hi = max(0, i - 120), min(n, i + 121)
        if cs[i] >= 0.6 * cs[lo:hi].max():
            keep.append(ds[i])
    return keep


def st_codes() -> set[str]:
    """ST 名单（stock_names 里名字带 ST/*ST 的代码）。"""
    con = sqlite3.connect(str(APP))
    try:
        return {str(r[0]) for r in con.execute(
            "SELECT code FROM stock_names WHERE name LIKE '%ST%'")}
    finally:
        con.close()


def scan_stock(dates: np.ndarray, o: np.ndarray, c: np.ndarray, h: np.ndarray,
               l: np.ndarray, v: np.ndarray, sig_pos: np.ndarray) -> dict:
    """对一只票、在给定信号日位置上，算箱体指标 + 各持有期收益（全部向量化）。

    返回等长数组（长度 = 信号日个数）；不满足门槛的位置填 NaN / 0。
    """
    n_sig = len(sig_pos)
    idx = sig_pos[:, None] - (BOX_WIN - 1) + np.arange(BOX_WIN)      # (n_sig, 750)
    win_c = c[idx]                                                    # 收盘窗口
    win_h = h[idx]
    win_l = l[idx]
    cur = win_c[:, -1]

    box_high = win_h.max(axis=1)
    box_low = win_l.min(axis=1)
    band = box_high - box_low
    with np.errstate(divide="ignore", invalid="ignore"):
        pos = np.where(band > 0, (cur - box_low) / band, np.nan)

    # 750 日分位（越低越低位）
    pct = (win_c <= cur[:, None]).sum(axis=1) / BOX_WIN * 100.0
    mean = win_c.mean(axis=1)
    std = win_c.std(axis=1)                    # 总体标准差，与 statistics.pstdev 同口径
    with np.errstate(divide="ignore", invalid="ignore"):
        cv = np.where(mean > 0, std / mean * 100.0, 999.0)

    # 线性回归年化斜率：num / denom * 252 / mean * 100
    x = np.arange(BOX_WIN, dtype=np.float64) - (BOX_WIN - 1) / 2.0
    denom = float((x ** 2).sum())
    num = win_c @ x
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = np.where(mean > 0, num / denom * 252.0 / mean * 100.0, np.nan)
        slope = np.where(np.isfinite(slope), slope, SLOPE_MAX)

    # 流动性：近 250 日均成交额（close×v）
    amt = (win_c[:, -250:] * v[idx][:, -250:]).mean(axis=1)

    # 触及次数：收盘落在箱顶/箱底 3% 带内的天数（上下沿任一）
    with np.errstate(divide="ignore", invalid="ignore"):
        t_hi = (win_c >= (box_high - 0.03 * band)[:, None]).sum(axis=1)
        t_lo = (win_c <= (box_low + 0.03 * band)[:, None]).sum(axis=1)
    touches = t_hi + t_lo

    # 谷底驻留：近 250 日落在箱体下 1/3 的天数占比
    edge = (box_low + band * DWELL_BOTTOM_RATIO)[:, None]
    dwell = (win_c[:, -DWELL_LOOKBACK:] <= edge).sum(axis=1) / float(DWELL_LOOKBACK)

    ok = ((box_low > 0) & (box_high > box_low) & (band > 0) & (amt >= MIN_AMOUNT_AVG)
          & (pct <= PCT_MAX) & (cv <= CV_MAX) & (np.abs(slope) <= SLOPE_MAX)
          & (touches >= TOUCH_MIN) & (dwell >= MIN_DWELL) & (cur > 0))

    out = {"ok": ok, "pos": pos, "dwell": dwell, "cv": cv, "pct": pct,
           "touches": touches, "slope": slope}
    for H in HOLDS:
        fwd = np.full(n_sig, np.nan)
        j = sig_pos + H
        valid = (j < len(c)) & ok
        fwd[valid] = c[j[valid]] / c[sig_pos[valid]] - 1.0 - COST
        out["f%d" % H] = fwd
    return out


def nw_t(x: np.ndarray, lag: int) -> tuple[float, float]:
    """Newey-West 修正的均值 t（窗口重叠必须修正，否则 t 会虚高 2~3 倍）。"""
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 8:
        return float("nan"), float("nan")
    m = x.mean()
    d = x - m
    var = float((d ** 2).sum()) / n
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        var += 2.0 * w * float((d[k:] * d[:-k]).sum()) / n
    if var <= 0:
        return m, float("nan")
    return m, m / math.sqrt(var / n)


def seg_stats(dates: list[int], series: np.ndarray, groups: int = 6) -> list[dict]:
    """把日序列切成若干段，各自算均值（看方向稳不稳）。"""
    n = len(dates)
    out = []
    step = max(1, n // groups)
    for i in range(0, n, step):
        sl = slice(i, min(i + step, n))
        s = series[sl]
        s = s[np.isfinite(s)]
        if len(s) < 5:
            continue
        out.append({"from": str(dates[i]), "to": str(dates[min(i + step, n) - 1]),
                    "n": int(len(s)), "mean": round(float(s.mean()) * 100, 3)})
    return out


def market_ret(con: sqlite3.Connection, d1: int, d2: int) -> float | None:
    """等权全市场从 d1 收盘到 d2 收盘的收益率（%，用来给每段贴行情标签）。"""
    rows = con.execute(
        "SELECT AVG(b.c / a.c) FROM kline a JOIN kline b ON a.code=b.code AND b.d=?"
        " WHERE a.d=? AND a.c>0 AND b.c>0", (d2, d1)).fetchone()
    v = rows[0] if rows else None
    return None if v is None else round((float(v) - 1.0) * 100, 2)


def block_t(x: np.ndarray, stride: int) -> tuple[float, int, float]:
    """非重叠抽样 t：每 stride 天取一个，窗口不重叠，t 不再被高估。"""
    s = x[::max(1, stride)]
    s = s[np.isfinite(s)]
    n = len(s)
    if n < 4:
        return float("nan"), n, float("nan")
    m = float(s.mean())
    sd = float(s.std(ddof=1))
    if sd <= 0:
        return m, n, float("nan")
    return m, n, m / (sd / math.sqrt(n))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2012-01-01",
                    help="默认拉到 2010：T+120 需要足够多的独立样本，3 年只有 6 个")
    ap.add_argument("--until", default="2026-11-04")
    ap.add_argument("--stamp", default=datetime.now().strftime("%Y%m%d"))
    args = ap.parse_args()

    con = sqlite3.connect(str(QFQ))
    sig_dates = load_signal_dates(con, args.since, args.until)
    if not sig_dates:
        print("没有可用信号日")
        return
    sig_set = set(sig_dates)
    print("信号日 %d 个：%d ~ %d" % (len(sig_dates), sig_dates[0], sig_dates[-1]))

    st = st_codes()
    # 起算日要往前留够 750 根日线（≈1200 个自然日），否则最早的一批信号日
    # 全都因为窗口不足被剔掉，白少两三年样本。
    base = datetime.strptime(args.since, "%Y-%m-%d") - timedelta(days=1300)
    d0 = int(base.strftime("%Y%m%d"))
    d1 = int(args.until.replace("-", ""))
    codes = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT code FROM kline WHERE d >= ? AND d <= ?", (d0, d1)).fetchall()]
    print("候选票 %d 只（含 ST %d 个代码，剔除后 %d）"
          % (len(codes), len(st), len([c for c in codes if c not in st])))

    CHUNK = 500          # 分块算，避免 (信号日×750) 的大数组把内存顶爆
    rows: list[dict] = []
    done = 0
    for code in codes:
        if code in st:
            continue
        q = con.execute(
            "SELECT d,o,c,h,l,v FROM kline WHERE code=? AND d>=? AND d<=? ORDER BY d",
            (code, d0, d1)).fetchall()
        if len(q) < MIN_BARS + 5:
            continue
        arr = np.array([(r[0], r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0, r[5] or 0)
                        for r in q], dtype=np.float64)
        dates = arr[:, 0].astype(np.int64)
        pos_of = {int(d): i for i, d in enumerate(dates)}
        # 必须留够 750 根窗口：sig_pos < 749 会让 idx 变负、numpy 从末尾绕回来
        # 拿未来数据算箱体（静默污染）。这里直接剔掉。
        use_dates = [d for d in sig_dates if d in pos_of and pos_of[d] >= BOX_WIN - 1]
        if not use_dates:
            continue
        o_, c_, h_, l_, v_ = arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5]
        for i0 in range(0, len(use_dates), CHUNK):
            ud = use_dates[i0:i0 + CHUNK]
            sp = np.array([pos_of[d] for d in ud], dtype=np.int64)
            m = scan_stock(dates, o_, c_, h_, l_, v_, sp)
            for k, d in enumerate(ud):
                if not m["ok"][k]:
                    continue
                row = {"date": int(d), "code": code,
                       "pos": float(m["pos"][k]), "dwell": float(m["dwell"][k]),
                       "touches": int(m["touches"][k])}
                for H in HOLDS:
                    v = m["f%d" % H][k]
                    row["f%d" % H] = None if not np.isfinite(v) else float(v)
                rows.append(row)
        done += 1
        if done % 500 == 0:
            print("  已扫 %d 只，命中 %d 条" % (done, len(rows)))

    con.close()
    print("扫完 %d 只，箱体池样本 %d 条" % (done, len(rows)))
    if not rows:
        print("池子为空，检查门槛")
        return

    # ---- 分组成日序列 ----
    by_day: dict[int, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
    dates_sorted = sorted(by_day)
    print("有效信号日 %d 个，日均池子 %.1f 只"
          % (len(dates_sorted), np.mean([len(by_day[d]) for d in dates_sorted])))

    report = {}
    diff_series: dict[int, np.ndarray] = {}
    days_kept: dict[int, list[int]] = {}
    for H in HOLDS:
        bot, rest, diff_b_r, kept = [], [], [], []
        for d in dates_sorted:
            grp = by_day[d]
            b = [x["f%d" % H] for x in grp if x["pos"] <= DWELL_BOTTOM_RATIO and x["f%d" % H] is not None]
            o = [x["f%d" % H] for x in grp if x["pos"] > DWELL_BOTTOM_RATIO and x["f%d" % H] is not None]
            if len(b) < 5 or len(o) < 5:      # 池子太薄的日子不算，噪声
                continue
            mb, mo = float(np.mean(b)), float(np.mean(o))
            bot.append(mb); rest.append(mo)
            diff_b_r.append(mb - mo); kept.append(d)
        bot = np.array(bot); rest = np.array(rest); diff_b_r = np.array(diff_b_r)
        diff_series[H] = diff_b_r
        days_kept[H] = kept
        if len(diff_b_r) == 0:
            report["T+%d" % H] = {"n_days": 0}
            continue
        m1, t1 = nw_t(diff_b_r, max(1, H // 5))
        # 非重叠抽样：窗口不重叠，t 才是真的 t（重叠会让 NW 也估不准）
        bm, bn, bt = block_t(diff_b_r, H)
        mkt_proxy = float(np.mean(rest))     # 对照组的平均收益 ≈ 同期行情
        report["T+%d" % H] = {
            "n_days": int(len(bot)),
            "bottom_mean_pct": round(float(bot.mean()) * 100, 3),
            "rest_mean_pct": round(float(rest.mean()) * 100, 3),
            "diff_pct": round(m1 * 100, 3),
            "nw_t": round(t1, 2),
            "block_n": int(bn),
            "block_diff_pct": None if not np.isfinite(bm) else round(bm * 100, 3),
            "block_t": None if not np.isfinite(bt) else round(bt, 2),
            "hit_rate_pct": round(float((diff_b_r > 0).mean()) * 100, 1),
            "segments": seg_stats(days_kept[H], diff_b_r),
        }

    # 池子整体 vs 全市场（参考，看箱体票本身是不是跑输）
    con = sqlite3.connect(str(QFQ))
    for H in HOLDS:
        if not report.get("T+%d" % H, {}).get("n_days"):
            continue
        vals = []
        # 必须用 days_kept 而不是 dates_sorted：和池内比较、和市场基准保持同一批日子，
        # 否则「池 vs 市场」的两边样本不同，差值就不是同一个东西。
        for d in days_kept.get(H) or dates_sorted:
            grp = by_day[d]
            p = [x["f%d" % H] for x in grp if x["f%d" % H] is not None]
            if len(p) < 5:
                continue
            vals.append(float(np.mean(p)))
        if vals:
            report["T+%d" % H]["pool_mean_pct"] = round(float(np.mean(vals)) * 100, 3)

    # 每段贴行情标签：同一段时间、大盘等权涨了多少（看边缘是不是行情送的）
    for H in HOLDS:
        r = report.get("T+%d" % H, {})
        segs = r.get("segments") or []
        for s in segs:
            s["market_pct"] = market_ret(con, int(s["from"]), int(s["to"]))
    con.close()

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / ("cond_box_%s.json" % args.stamp)
    payload = {"meta": {"since": args.since, "until": args.until,
                        "n_signal_days": len(sig_dates), "n_pool_rows": len(rows),
                        "n_stocks_scanned": done, "min_dwell": MIN_DWELL,
                        "cost": COST, "src": "qfq"},
               "report": report,
               # 留出参与统计的日子，另一支脚本用它们算同期的全市场等权基准
               "days_by_hold": {str(H): days_kept[H] for H in HOLDS if H in days_kept}}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n== 箱体底部：池内区分力（触发组 − 同池未触发组）==")
    print("   注：池内两组同扣成本，差值里已抵消；NW t 用重叠窗口，偏松；block t 为非重叠抽样，偏严。")
    for H in HOLDS:
        r = report.get("T+%d" % H, {})
        if not r.get("n_days"):
            print("T+%-3d 样本不足" % H)
            continue
        print("T+%-3d 池 %d 日 | 底部组 %+.3f%% | 其余 %+.3f%% | 差 %+.3f%%"
              " | NW t %+.2f | 非重叠 n=%d 差 %s%% t %s | 差>0 %.1f%%"
              % (H, r["n_days"], r["bottom_mean_pct"] or 0, r["rest_mean_pct"] or 0,
                 r["diff_pct"], r["nw_t"], r["block_n"],
                 r["block_diff_pct"], r["block_t"], r["hit_rate_pct"]))

    print("\n== 分段：边缘 vs 同期行情（看是不是行情送的）==")
    for H in HOLDS:
        r = report.get("T+%d" % H, {})
        if not r.get("n_days"):
            continue
        print("-- T+%d --" % H)
        for s in r["segments"]:
            print("  %s~%s n=%3d | 边缘 %+7.3fpp | 同期大盘 %s%%"
                  % (s["from"], s["to"], s["n"], s["mean"],
                     "n/a" if s.get("market_pct") is None else "%+.1f" % s["market_pct"]))
    print("\n写盘：%s" % path)


if __name__ == "__main__":
    main()
