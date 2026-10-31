# -*- coding: utf-8 -*-
"""分状态结论的稳健性检验 + 有效样本量（2026-10-29）。

为什么必须先做这一步
--------------------
`signal_backtest` 给出的「分状态基线」看起来很有说服力：
    前向20日：强势 -1.32% ｜ 震荡 -0.27% ｜ 弱势 +0.71%
读起来像"弱势市之后 20 日必反弹"。但这个数字有两个陷阱，不拆掉就不能拿它改权重：

  陷阱1｜样本重叠。T+20 的前向收益，相邻两个交易日的窗口重叠 19/20。一行 49 万
        的样本表，真正**不重叠**的观测只有 288/20 ≈ 14 个（= 交易日数 / 前向天数）。
        拿 n=491486 去算显著性，等于把同一段行情数了 20 遍。
        本脚本统一报告「有效窗口数 = 交易日数 / fwd」，这才是该看的自由度。

  陷阱2｜样本期单一。数据只有 2025-08 ~ 2026-10 共 288 个交易日，涵盖的可能只是
        一轮"急跌—急涨"。若把样本切成前后两半，两半的符号不一致，那"弱势反弹"
        就是这一段行情的特征，不是可复用的规律。

输出三件事
----------
1) 分状态基线 + 信号超额，在**前半段 / 后半段 / 全样本**三个切法下是否同号；
2) 每个结论的**有效独立窗口数**，并据此给出"可信 / 仅供方向参考 / 不足以下结论"；
3) 一个明确的门槛：有效窗口 < 20 时，任何 T+20 结论都不写进权重。

用法：
    .venv/Scripts/python.exe tools/regime_robust.py
    .venv/Scripts/python.exe tools/regime_robust.py --fwd 5 10 20
"""
import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")
REGIME_CN = {"up": "强势", "range": "震荡", "down": "弱势"}
REGIMES = ("up", "range", "down")
MIN_EFF_WINDOWS = 20      # 有效独立窗口少于这个数，不写进权重


def _eff_windows(n_days: int, fwd: int) -> float:
    """有效独立窗口数：交易日数 / 前向天数（非重叠分段）。"""
    return n_days / float(fwd)


def _grade(eff: float) -> str:
    if eff >= 60:
        return "可信"
    if eff >= MIN_EFF_WINDOWS:
        return "仅供方向参考"
    return "不足以下结论"


def load_panel(con, fwds):
    df = pd.read_sql("SELECT date AS trade_date, code, close FROM stock_indicators_daily "
                     "WHERE close IS NOT NULL AND close > 0", con)
    df = df.sort_values(["code", "trade_date"], kind="mergesort")
    g = df.groupby("code", sort=False)["close"]
    for f in fwds:
        df["f%d" % f] = g.shift(-f) / df["close"] - 1.0
        df.loc[df["f%d" % f].abs() > 0.5, "f%d" % f] = np.nan   # 停牌跨越/复权断层
    reg = pd.read_sql("SELECT trade_date, regime FROM market_regime", con)
    df = df.merge(reg, on="trade_date", how="left")
    df["regime"] = df["regime"].fillna("range")
    return df


def summarize(df, fwds):
    dates = sorted(df["trade_date"].unique())
    half = len(dates) // 2
    d1, d2 = set(dates[:half]), set(dates[half:])
    df = df.assign(
        seg=np.where(df["trade_date"].isin(d1), "前半段",
                     np.where(df["trade_date"].isin(d2), "后半段", "?")))
    out = {}
    for f in fwds:
        col = "f%d" % f
        sub = df[df[col].notna()]
        out[f] = {
            "ndays": int(sub["trade_date"].nunique()),
            "eff": _eff_windows(sub["trade_date"].nunique(), f),
            "all": {}, "h1": {}, "h2": {},
        }
        for reg in REGIMES:
            s = sub[sub["regime"] == reg]
            if s.empty:
                continue
            out[f]["all"][reg] = float(s[col].mean())
            for seg, key in (("前半段", "h1"), ("后半段", "h2")):
                ss = s[s["seg"] == seg]
                if len(ss):
                    out[f][key][reg] = float(ss[col].mean())
    return out


def main(fwds):
    con = sqlite3.connect(DB, timeout=30)
    try:
        df = load_panel(con, fwds)
    finally:
        con.close()
    res = summarize(df, fwds)

    print("=" * 78)
    print("分状态基线：全样本 vs 前后两半（税后口径另需减 0.15%，此处为税前均值）")
    print("=" * 78)
    verdict = {}
    for f in fwds:
        r = res[f]
        print("\n前向%d日 ｜ 交易日 %d ｜ 有效独立窗口 %.1f 个 → 结论等级：%s"
              % (f, r["ndays"], r["eff"], _grade(r["eff"])))
        print("  %-6s %10s %10s %10s   %s" % ("状态", "全样本", "前半段", "后半段", "同号?"))
        for reg in REGIMES:
            a = r["all"].get(reg)
            h1 = r["h1"].get(reg)
            h2 = r["h2"].get(reg)
            if a is None:
                continue
            def _p(x):
                return "%+9.2f%%" % (x * 100) if x is not None else "%10s" % "—"
            same = "—"
            if h1 is not None and h2 is not None:
                same = "一致" if (h1 > 0) == (h2 > 0) else "**翻转**"
            print("  %-6s %s %s %s   %s" % (REGIME_CN[reg], _p(a), _p(h1), _p(h2), same))
        # 给出这个前向期的核心判据
        signs = [1 if r["all"].get(k, 0) > 0 else -1 for k in REGIMES]
        strong = r["all"].get("down")
        verdict[f] = {
            "eff": r["eff"], "grade": _grade(r["eff"]),
            "down_ret": strong,
            "monotone": all(signs[i] <= signs[i + 1] for i in range(len(signs) - 1)),
        }
        if strong is not None:
            print("  → 弱势档均值 %+.2f%%%s"
                  % (strong * 100, "（三档单调，方向规律清晰）" if verdict[f]["monotone"] else ""))

    print("\n" + "=" * 78)
    print("结论门槛")
    print("=" * 78)
    ok = [f for f in fwds if verdict[f]["eff"] >= MIN_EFF_WINDOWS and verdict[f]["grade"] == "可信"]
    ref = [f for f in fwds if f not in ok and verdict[f]["eff"] >= MIN_EFF_WINDOWS]
    weak = [f for f in fwds if f not in ok and f not in ref]
    print("  可信（可写进权重）：%s" % (", ".join("T+%d" % f for f in ok) or "无"))
    print("  仅方向参考（可展示、不改权重）：%s" % (", ".join("T+%d" % f for f in ref) or "无"))
    print("  不足以下结论（不展示结论、只标样本不足）：%s"
          % (", ".join("T+%d" % f for f in weak) or "无"))
    print("\n  说明：有效独立窗口 = 交易日数 / 前向天数。因为相邻交易日的 fwd 日窗口重叠")
    print("  fwd-1 天，n 笔样本并不等于 n 个独立观测；把同一段行情数 20 遍会制造伪显著。")

    # 三档极性：逆势类 vs 追涨类
    print("\n" + "=" * 78)
    print("顺带核一件事：同一信号在不同状态下的超额方向是否稳定翻转")
    print("=" * 78)
    print("  见 signal_backtest 控制台「三档一致性」表；若某信号在强势/弱势两档符号相反，")
    print("  说明它对市况敏感，正确用法是**按当前状态启用**，而不是给一个固定权重。")
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fwd", type=int, nargs="+", default=[5, 10, 20])
    a = ap.parse_args()
    main(tuple(a.fwd))
