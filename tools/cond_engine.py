# -*- coding: utf-8 -*-
"""条件化回测引擎 —— 一次算指标，所有工具共用 —— 2026-11-05

## 它解决的到底是什么

`cond_box_backtest.py` 只测了箱体一个工具。要铺到 30 个工具，每个都重写一遍箱体那套
是浪费，而且口径一定会走偏。所以这里把**共用的部分**抽出来：

  1. 每只票读一次日线、算一次指标（用 `viewer/indicators.py` 的 `compute_indicators`，
     与产品指标表同一套代码，不另写一份）
  2. 前置池 = 「全市场可交易」：收盘>0、成交量>0、成交额≥500万、指标已过预热期
  3. 每个工具一个布尔判据，判据口径**逐条照抄 `toolbox.py` 里的实现**（见 TOOLS 注释）
  4. 每天在池内分成「触发 / 未触发」两组，算 H 日收益差
  5. 重叠窗口用 NW t，非重叠抽样用 block t，再切 6 段看方向稳不稳

## 为什么前置池是「全市场」而不是某个子池

这批工具在筛台里的实际用法就是「第一级从全市场开始」。所以它们的池子就是全市场。
箱体那条特殊（它自带箱体前置池），已经单独测过，不在这里重复。

## 窗口跟着持有期走

试点实证：同一工具 T+120，3 年窗口给 +1.894pp、14.8 年给 −1.594pp，**符号相反**。
所以：
  H ≤ 5 日  → 2021-01-01 起（约 5 年，结构最近，独立样本够）
  H ≤ 60 日 → 2015-01-01 起（约 11 年）
  其余      → 2012-01-01 起（14.8 年）

## 成交可执行性

触发器里剔掉「收盘涨停」的日子——涨停收盘买不进，留着等于虚增收益。
涨停门槛按代码段：300/301/688/689 → 19.8%，4xx/8xx/920 → 29.8%，其余 9.8%。
（ST 实际 5%，库里有历史 ST 状态，无法逐日还原，这条口径偏松，已记在局限里。）

## 用法
  python tools/cond_engine.py --batch all
  python tools/cond_engine.py --batch short --limit 300   # 调试
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sqlite3
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from viewer.indicators import compute_indicators  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
QFQ = ROOT / "data" / "kline_daily.db"
OUT = ROOT / "viewer" / "logs"

DATA_START = 20100101        # 取数起点（留 2 年预热给 ma200）
COST = 0.0025
MIN_AMOUNT = 5.0e6           # 前置池：当日成交额 ≥500 万（可交易）
MIN_BARS = 260               # 指标预热
HOLDS = (1, 3, 5, 10, 20, 60, 120)


def win_start(h: int) -> int:
    if h <= 5:
        return 20210101
    if h <= 60:
        return 20150101
    return 20120101


def limit_thr(code: str) -> float:
    """涨停门槛（近似），与 `toolbox.py:_limit_up_thr` 同口径。"""
    c = str(code or "")
    if c[:3] in ("300", "301", "688", "689"):
        return 19.8
    if c[:1] in ("4", "8") or c[:3] == "920":
        return 29.8
    return 9.8


# ---------------------------------------------------------------- 工具判据
# 每条都写明「照抄哪里」，改判据必须同步那边，否则卡片上的说明会和回测对不上。

def _t_ma_bull(X):
    """策略模板 ma_bull_array：close>ma20>ma60>ma200 且 macd>0。"""
    return ((X["close"] > X["ma20"]) & (X["ma20"] > X["ma60"])
            & (X["ma60"] > X["ma200"]) & (X["macd"] > 0) & (X["ma200"] > 0))


def _t_ma_align(X):
    """`toolbox.py:_p_ma_align`：close>ma5>ma20>ma60（**不含 ma200**，与上面不同）。"""
    return (X["close"] > X["ma5"]) & (X["ma5"] > X["ma20"]) & (X["ma20"] > X["ma60"])


def _t_vol_break(X):
    """策略模板 vol_breakout：volume_ratio>2 且 close>ma20。"""
    return (X["volume_ratio"] > 2) & (X["close"] > X["ma20"])


def _t_burst(X):
    """`_p_burst`：当日涨幅 ≥4%。"""
    return X["ret1"] >= 4.0


def _t_burst_vol(X):
    """`_p_burst_vol`：涨幅 ≥4% 且量比 ≥1.5。"""
    return (X["ret1"] >= 4.0) & (X["volume_ratio"] >= 1.5)


def _t_shrink(X):
    """`_p_shrink`：量比 ≤0.7。"""
    return X["volume_ratio"] <= 0.7


def _t_rsi(X):
    """`_p_rsi`：rsi6 ≥ 80。"""
    return X["rsi6"] >= 80.0


def _t_mom(X):
    """`_p_mom`：20 日涨幅 ≥ +20%。"""
    return X["mom20"] >= 20.0


def _t_reversal(X):
    """`_p_reversal`：20 日涨幅 ≤ −20%。"""
    return X["mom20"] <= -20.0


def _t_high_vol(X):
    """`_p_high_vol`：atr/close ≥ 6%。"""
    return X["atr_pct"] >= 6.0


def _t_near_high(X):
    """`_p_near_high`（max_gap=25）：距 250 日最高收盘 ≤25% 以内。"""
    return X["dist_high250"] >= -25.0


def _t_near_high_tight(X):
    """`_p_near_high`（max_gap=5）。"""
    return X["dist_high250"] >= -5.0


def _t_pullback(X):
    """`_p_pullback`：距 MA20 在 −3%~0%。"""
    return (X["ma20_dev"] >= -3.0) & (X["ma20_dev"] <= 0.0)


def _t_oversold(X):
    """策略模板 oversold_rebound：rsi6<30 且 wr6<−80 且 cci<−100。"""
    return (X["rsi6"] < 30) & (X["wr6"] < -80) & (X["cci"] < -100)


# tool_key -> (判据, 持有期, 分类, 说明)
TOOLS: dict[str, dict] = {
    # 短线·追热
    "pick_vol_break": dict(fn=_t_vol_break, h=(1, 3, 5), grp="short_hot",
                           note="模板 vol_breakout（量比>2 且站上 MA20）"),
    "oss_burst_vol": dict(fn=_t_burst_vol, h=(1, 3, 5), grp="short_hot",
                          note="开源·涨幅≥4% 且量比≥1.5（advisory）"),
    "oss_burst": dict(fn=_t_burst, h=(1, 3, 5), grp="short_hot",
                      note="开源·纯涨幅≥4%（advisory）"),
    "oss_shrink_vol": dict(fn=_t_shrink, h=(1, 3, 5), grp="short_hot",
                           note="开源·量比≤0.7（advisory，已证纯风格）"),
    "oss_rsi_overbought": dict(fn=_t_rsi, h=(1, 3, 5), grp="short_hot",
                               note="开源·RSI6≥80（advisory）"),
    # 短线·超跌
    "pick_oversold": dict(fn=_t_oversold, h=(3, 5, 10, 20), grp="short_dip",
                          note="模板 oversold_rebound（rsi6<30 & wr6<-80 & cci<-100）"),
    "oss_reversal20": dict(fn=_t_reversal, h=(3, 5, 10, 20), grp="short_dip",
                           note="开源·20日跌幅≥20%（advisory）"),
    "oss_mom20": dict(fn=_t_mom, h=(3, 5, 10, 20), grp="short_dip",
                      note="开源·20日涨幅≥20%（advisory）"),
    "oss_pullback_ma20": dict(fn=_t_pullback, h=(3, 10, 20), grp="short_dip",
                              note="开源·距 MA20 −3%~0%（advisory）"),
    # 中线突破
    "pick_ma_bull": dict(fn=_t_ma_bull, h=(20, 60), grp="mid_break",
                         note="模板 ma_bull_array（含 ma200）"),
    "oss_ma_align": dict(fn=_t_ma_align, h=(20, 60), grp="mid_break",
                         note="开源·多头排列（**不含 ma200**，advisory）"),
    # 中线低位 / 长持
    "oss_near_high": dict(fn=_t_near_high, h=(20, 60), grp="mid_base",
                          note="开源·距 52 周高 ≤25%（advisory）"),
    "oss_near_high_tight": dict(fn=_t_near_high_tight, h=(20, 60), grp="mid_base",
                                note="开源·距 52 周高 ≤5%（advisory）"),
    # 闸门
    "oss_high_vol": dict(fn=_t_high_vol, h=(20,), grp="gate",
                         note="开源·ATR%≥6%（advisory，排除型）"),
    # ---- 负对照：每天随机抽 5% 的池成员当「触发」 ----
    # 它的差值必须 ≈ 0。如果它也跑出显著的负值，说明引擎（或口径）本身在制造
    # 假阴性，所有工具的结论全部作废。这一条不是可选项。
    "random5": dict(fn=lambda X: X["rand5"] < 0.05, h=(3, 20), grp="ctrl",
                    note="负对照·每天随机 5%（必须 ≈0）"),
}

# 判据库接入：写进 viewer/playbook.py 的方法自动出现在回测里，不需要再改这里
try:
    from viewer.playbook import as_tools as _pb_as_tools, enrich_X as _pb_enrich
    TOOLS.update(_pb_as_tools())
    _PB = True
except Exception as _e:      # noqa: BLE001
    _PB = False
    print("判据库未接入：%s" % _e)

BATCH = {
    "short": ("short_hot", "short_dip"),
    "playbook": ("pb_trend", "pb_form", "pb_sent"),
    "mid": ("mid_break", "mid_base"),
    "gate": ("gate",),
    "ctrl": ("ctrl",),
    "all": ("short_hot", "short_dip", "mid_break", "mid_base", "gate", "ctrl",
            "pb_trend", "pb_form", "pb_sent"),
}


def nw_t(x: np.ndarray, lag: int) -> tuple[float, float]:
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


def block_t(x: np.ndarray, stride: int) -> tuple[float, int, float]:
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


def seg_stats(dates: np.ndarray, series: np.ndarray, groups: int = 6) -> list[dict]:
    n = len(dates)
    out = []
    step = max(1, n // groups)
    for i in range(0, n, step):
        j = min(i + step, n)
        if j - i < 5:
            continue
        s = series[i:j]
        out.append({"from": str(int(dates[i])), "to": str(int(dates[j - 1])),
                    "n": int(j - i), "mean": round(float(np.nanmean(s)) * 100, 3)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default="all")
    ap.add_argument("--limit", type=int, default=0, help="只扫前 N 只（调试）")
    ap.add_argument("--stamp", default=datetime.now().strftime("%Y%m%d"))
    # 风格对照：把前置池卡在一条窄的成交额带里，市值/流动性差异被压掉，
    # 再看触发组是否仍然为负。若仍然为负 → 不是风格解释的。
    ap.add_argument("--amount-min", type=float, default=MIN_AMOUNT)
    ap.add_argument("--amount-max", type=float, default=0.0, help="0 表示不设上限")
    args = ap.parse_args()

    want_grp = BATCH[args.batch]
    tools = {k: v for k, v in TOOLS.items() if v["grp"] in want_grp}
    if not tools:
        print("没有匹配的工具")
        return
    print("本次测 %d 个工具、%d 个单元"
          % (len(tools), sum(len(v["h"]) for v in tools.values())))

    con = sqlite3.connect(str(QFQ))
    # 全局交易日轴
    dates_all = np.array([int(r[0]) for r in con.execute(
        "SELECT DISTINCT d FROM kline WHERE d>=? ORDER BY d", (DATA_START,))], dtype=np.int64)
    n_dates = len(dates_all)
    dpos = {int(d): i for i, d in enumerate(dates_all)}
    print("全局交易日 %d 个：%d ~ %d" % (n_dates, dates_all[0], dates_all[-1]))

    codes = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT code FROM kline WHERE d>=? AND d<=20261104", (DATA_START,))]
    if args.limit:
        codes = codes[:args.limit]
    print("候选票 %d 只" % len(codes))

    # 累加器：pool 与「触发组」各自的 sum/count（按全局日轴）
    # 未触发组均值由 (pool − trig) / (cnt_pool − cnt_trig) 反推，省一半内存。
    pool = {H: (np.zeros(n_dates), np.zeros(n_dates)) for H in HOLDS}
    acc = {(k, H): (np.zeros(n_dates), np.zeros(n_dates))
           for k, v in tools.items() for H in v["h"]}

    t0 = time.time()
    done = 0
    for code in codes:
        q = con.execute(
            "SELECT d,o,c,h,l,v FROM kline WHERE code=? AND d>=? AND d<=20261104 ORDER BY d",
            (code, DATA_START)).fetchall()
        if len(q) < MIN_BARS + 2:
            continue
        df = pd.DataFrame(q, columns=["d", "open", "close", "high", "low", "volume"])
        df["date"] = pd.to_datetime(df["d"].astype(str), format="%Y%m%d")
        df["code"] = code
        try:
            ind = compute_indicators(df)
        except Exception:      # noqa: BLE001  单只票算崩不该拖垮整轮
            continue

        c = df["close"].to_numpy(np.float64)
        v = df["volume"].to_numpy(np.float64)
        prev = np.concatenate(([np.nan], c[:-1]))
        with np.errstate(divide="ignore", invalid="ignore"):
            ret1 = (c / prev - 1.0) * 100.0
            mom20 = (c / np.concatenate((np.full(20, np.nan), c[:-20])) - 1.0) * 100.0
            high250 = pd.Series(c).rolling(251).max().to_numpy(np.float64)
            dist_high250 = (c / high250 - 1.0) * 100.0
            ma20 = ind["ma20"].to_numpy(np.float64)
            ma20_dev = (c / ma20 - 1.0) * 100.0
            atr_pct = ind["atr"].to_numpy(np.float64) / c * 100.0
        X = dict(close=c, volume=v, ret1=ret1, mom20=mom20,
                 dist_high250=dist_high250, ma20_dev=ma20_dev, atr_pct=atr_pct,
                 ma5=ind["ma5"].to_numpy(np.float64),
                 ma20=ma20,
                 ma60=ind["ma60"].to_numpy(np.float64),
                 ma200=ind["ma200"].to_numpy(np.float64),
                 macd=ind["macd"].to_numpy(np.float64),
                 rsi6=ind["rsi6"].to_numpy(np.float64),
                 wr6=ind["wr6"].to_numpy(np.float64),
                 cci=ind["cci"].to_numpy(np.float64),
                 volume_ratio=ind["volume_ratio"].to_numpy(np.float64))
        thr = limit_thr(code)
        # 负对照用的随机数：按代码做稳定种子（不用内置 hash，它每次进程都变）
        X["rand5"] = np.random.default_rng(
            zlib.crc32(code.encode("utf-8")) % (2 ** 32)).random(len(c))
        if _PB:
            _pb_enrich(X, df, ind)
            X["_limit_thr"] = thr

        # 前置池：可交易 + 指标已预热 + 收盘不涨停（涨停买不进）
        amt = c * v
        thr = limit_thr(code)
        base = (np.isfinite(c) & (c > 0) & np.isfinite(v) & (v > 0)
                & (amt >= args.amount_min) & np.isfinite(X["ma200"]) & np.isfinite(ret1)
                & (ret1 < thr - 0.2))
        if args.amount_max:
            base = base & (amt <= args.amount_max)
        if not base.any():
            continue

        gpos = np.array([dpos.get(int(d), -1) for d in df["d"].to_numpy()], dtype=np.int64)
        keep = gpos >= 0
        if not keep.all():
            base = base & keep
            gpos = np.where(keep, gpos, 0)
        idx_base = gpos[base]

        # 各持有期的前向收益（用复权收盘，已扣一次成本；两组同扣，差值里抵消）
        fwd = {}
        for H in HOLDS:
            f = np.full(len(c), np.nan)
            if len(c) > H:
                with np.errstate(divide="ignore", invalid="ignore"):
                    f[:-H] = c[H:] / c[:-H] - 1.0 - COST
            fwd[H] = f
            ok = base & np.isfinite(f)
            if not ok.any():
                continue
            # 用 bincount 而不是 np.add.at：后者是逐元素的慢路径，
            # 5000 只票 × 7 个持有期跑下来会从几秒变成几分钟。
            pool[H][0][:] += np.bincount(gpos[ok], weights=f[ok], minlength=n_dates)
            pool[H][1][:] += np.bincount(gpos[ok], minlength=n_dates)

        for k, spec in tools.items():
            mask = spec["fn"](X)
            for H in spec["h"]:
                ok = mask & base & np.isfinite(fwd[H])
                if not ok.any():
                    continue
                acc[(k, H)][0][:] += np.bincount(gpos[ok], weights=fwd[H][ok], minlength=n_dates)
                acc[(k, H)][1][:] += np.bincount(gpos[ok], minlength=n_dates)

        done += 1
        if done % 400 == 0:
            print("  已扫 %d 只（%.0fs）" % (done, time.time() - t0))
    con.close()
    print("扫完 %d 只，用时 %.0fs" % (done, time.time() - t0))

    # ---------------- 汇总 ----------------
    report: dict[str, dict] = {}
    for k, spec in tools.items():
        for H in spec["h"]:
            st, ct = acc[(k, H)]
            sp, cp = pool[H]
            with np.errstate(divide="ignore", invalid="ignore"):
                m_t = np.where(ct > 0, st / np.maximum(ct, 1), np.nan)
                m_u = np.where(cp - ct > 0, (sp - st) / np.maximum(cp - ct, 1), np.nan)
            diff = m_t - m_u
            sel = (dates_all >= win_start(H)) & np.isfinite(diff) & (ct >= 5) & (cp - ct >= 5)
            if sel.sum() < 20:
                report["%s|T+%d" % (k, H)] = {"n_days": int(sel.sum()), "note": "样本不足"}
                continue
            dd = diff[sel]
            ds = dates_all[sel]
            m1, t1 = nw_t(dd, max(1, H // 5))
            bm, bn, bt = block_t(dd, H)
            report["%s|T+%d" % (k, H)] = {
                "tool": k, "hold": H, "grp": spec["grp"], "note": spec["note"],
                "n_days": int(len(dd)),
                "trig_mean_pct": round(float(np.nanmean(m_t[sel])) * 100, 3),
                "untrig_mean_pct": round(float(np.nanmean(m_u[sel])) * 100, 3),
                "diff_pct": round(m1 * 100, 3),
                "nw_t": round(t1, 2),
                "block_n": int(bn),
                "block_diff_pct": None if not np.isfinite(bm) else round(bm * 100, 3),
                "block_t": None if not np.isfinite(bt) else round(bt, 2),
                "hit_rate_pct": round(float((dd > 0).mean()) * 100, 1),
                "avg_trig_per_day": round(float(ct[sel].mean()), 1),
                "segments": seg_stats(ds, dd),
            }

    OUT.mkdir(parents=True, exist_ok=True)
    tag = args.batch + ("_band" if args.amount_max else "")
    path = OUT / ("cond_engine_%s_%s.json" % (tag, args.stamp))
    path.write_text(json.dumps({"meta": {"batch": args.batch, "stamp": args.stamp,
                                         "n_stocks": done, "data_start": DATA_START,
                                         "min_amount": args.amount_min,
                                         "max_amount": args.amount_max, "cost": COST,
                                         "exclude_limit_up": True,
                                         "windows": {str(h): win_start(h) for h in HOLDS}},
                                "report": report},
                               ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n== 条件化回测：触发组 − 同池未触发组（全市场池）==")
    print("%-22s %-5s %8s %9s %10s %7s %6s %7s %6s %6s"
          % ("工具", "持有", "触发均", "未触发均", "差(pp)", "NW t", "非重叠n", "非重叠t", "日均触发", "差>0%"))
    for key in sorted(report, key=lambda x: (report[x].get("grp", ""), x)):
        r = report[key]
        if r.get("note") == "样本不足":
            print("%-22s %-5s 样本不足 n=%d" % (r.get("tool", key), r.get("hold", ""), r["n_days"]))
            continue
        print("%-22s T+%-3d %+8.3f %+9.3f %+10.3f %+7.2f %6d %+7s %6.0f %6.1f"
              % (r["tool"], r["hold"], r["trig_mean_pct"], r["untrig_mean_pct"],
                 r["diff_pct"], r["nw_t"], r["block_n"],
                 "n/a" if r["block_t"] is None else "%+.2f" % r["block_t"],
                 r["avg_trig_per_day"], r["hit_rate_pct"]))
    print("\n写盘：%s" % path)


if __name__ == "__main__":
    main()
