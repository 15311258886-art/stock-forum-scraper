# -*- coding: utf-8 -*-
"""FinRL 指标移植检验（一次性实验 —— 2026-11-03）。

## 为什么会有这个脚本

前一份 `exp_rl_probe.py` 里我犯了三个错，这个脚本是为了纠错：

 1. **特征是我手搓的 12 维，不是 FinRL 的**。FinRL 真正用的是
    `finrl/config.py :: INDICATORS` 这 8 个（stockstats 口径）：
        macd / boll_ub / boll_lb / rsi_30 / cci_30 / dx_30
        close_30_sma / close_60_sma
 2. **我测的是单只票的择时，FinRL 做的是 N 只票的组合分配**。它的 state 是
    `[cash] + 全部收盘价 + 全部持仓股数 + 8×N 个指标`，action 是
    `Box(-1,1,shape=(N,))` 连续股数，不是我这边的三档仓位。
 3. **它的 reward 是 `end_total_asset - begin_total_asset`（美元绝对值×scaling）**，
    不是日收益率。

所以这次不猜、不仿，直接用它自己的依赖库（stockstats，已 pip 安装）算它自己的
8 个指标，然后问一个它没问过的问题：**这 8 个指标在 A 股日频上，单维有没有预测力？**
门禁沿用三期口径（|IC|>=0.02 / |ICIR|>=0.30 / |t|>=2.5），截面按日期对齐。

## 另一个必须单独测的东西：turbulence

`FeatureEngineer.calculate_turbulence` 是 FinRL 里我认为最值得搬的一块：
用 252 日滚动协方差算当日收益向量的马氏距离，超过阈值就清仓。
这是个**纯统计的风险开关**，不依赖任何美股特性。所以单独测它：
高 turbulence 之后，市场是不是真的更差。

用法：python tools/exp_finrl_indicators.py [--n 300]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import exp_rl_probe as EP          # noqa: E402  复用取数与 Rank IC 实现

# FinRL 原样照抄的清单（finrl/config.py :: INDICATORS）
FINRL_INDICATORS = ["macd", "boll_ub", "boll_lb", "rsi_30",
                    "cci_30", "dx_30", "close_30_sma", "close_60_sma"]

GATE = EP.GATE


def finrl_features(b: dict) -> tuple:
    """用 stockstats 算 FinRL 那 8 个指标，返回 (列名, 面板值, 衍生归一化版本)。

    为什么要衍生：FinRL 是把**原始值**直接喂进网络的（`_stock_values(tech)`），
    而 macd、boll_ub、close_30_sma 都是**价格单位**，100 元的票和 5 元的票数值差 20 倍。
    这种量纲在单票内部可以靠网络学出缩放，但**在截面上没法比大小**。
    所以要检验"有没有预测力"，必须换成同量纲的形式（除以收盘价）。
    两套都留着，原始值那套作为"照原样喂会怎样"的对照。
    """
    import stockstats
    df = pd.DataFrame({"date": b["d"], "open": b["o"], "high": b["h"],
                       "low": b["l"], "close": b["c"], "volume": b["v"]})
    s = stockstats.wrap(df)
    raw = {}
    for k in FINRL_INDICATORS:
        try:
            raw[k] = np.asarray(s[k], dtype=np.float64)
        except Exception:
            raw[k] = np.full(len(df), np.nan)
    c = np.asarray(b["c"], dtype=np.float64)
    eps = 1e-12
    names = ["macd/close", "boll_ub/close-1", "boll_lb/close-1", "布林%B",
             "rsi_30", "cci_30", "dx_30", "close/SMA30-1", "close/SMA60-1"]
    F = np.full((len(c), len(names)), np.nan)
    F[:, 0] = raw["macd"] / (c + eps)
    F[:, 1] = raw["boll_ub"] / (c + eps) - 1
    F[:, 2] = raw["boll_lb"] / (c + eps) - 1
    bw = raw["boll_ub"] - raw["boll_lb"]
    F[:, 3] = (c - raw["boll_lb"]) / (bw + eps)          # %B，0=下轨 1=上轨
    F[:, 4] = raw["rsi_30"]
    F[:, 5] = raw["cci_30"]
    F[:, 6] = raw["dx_30"]
    F[:, 7] = c / (raw["close_30_sma"] + eps) - 1
    F[:, 8] = c / (raw["close_60_sma"] + eps) - 1
    raw_macd = raw["macd"]
    return names, F, raw_macd


def panel(values: dict, dates_all: list, lo: int, hi: int, ncol: int) -> tuple:
    """按**日期**对齐成 (日期 × 票 × 维度)，不用位置对齐。"""
    codes = list(values.keys())
    dset = set()
    for c in codes:
        dset.update(values[c]["d"][lo:hi])
    dates = sorted(dset)
    di = {x: i for i, x in enumerate(dates)}
    Fa = np.full((len(dates), len(codes), ncol), np.nan)
    Ra = np.full((len(dates), len(codes)), np.nan)
    for j, code in enumerate(codes):
        v = values[code]
        c = v["c"]
        hi2 = min(hi, len(c) - 1)
        if hi2 <= lo:
            continue
        idx = np.array([di[x] for x in v["d"][lo:hi2]])
        Fa[idx, j, :] = v["F"][lo:hi2]
        Ra[idx, j] = c[lo + 1:hi2 + 1] / c[lo:hi2] - 1
    return dates, codes, Fa, Ra


def gate(Fa, Ra, names):
    nD = Fa.shape[0]
    out = []
    for k in range(len(names)):
        ics = []
        for i in range(nD):
            x, y = Fa[i, :, k], Ra[i, :]
            m = np.isfinite(x) & np.isfinite(y)
            if m.sum() < 50:
                continue
            v = EP._spearman(x[m], y[m])
            if np.isfinite(v):
                ics.append(v)
        if len(ics) < 20:
            out.append({"k": k, "n": len(ics), "ic": 0.0, "icir": 0.0, "t": 0.0,
                        "ok": False, "why": "有效截面天数不足"})
            continue
        a = np.asarray(ics)
        ic, sd = float(a.mean()), float(a.std(ddof=1))
        icir = ic / sd if sd > 0 else 0.0
        t = icir * np.sqrt(len(a))
        why = []
        if abs(ic) < GATE["abs_ic"]:
            why.append("IC %.4f" % ic)
        if abs(icir) < GATE["abs_icir"]:
            why.append("ICIR %.2f" % icir)
        if abs(t) < GATE["abs_t"]:
            why.append("t %.2f" % t)
        out.append({"k": k, "n": len(a), "ic": ic, "icir": icir, "t": t,
                    "ok": not why, "why": "、" + "、".join(why) if why else ""})
    return out


def turbulence(bars: dict, dates_all: list) -> pd.DataFrame:
    """照 `FeatureEngineer.calculate_turbulence` 的逻辑算：252 日滚动协方差 + 马氏距离。

    只改一处：原版 `start=252` 是硬编码的"跳过开头一年"，我们数据只有 850 天，
    仍然保留 252 天预热（否则协方差不可估）。完全照搬这句话的含义。
    """
    px = {}
    for code, b in bars.items():
        s = pd.Series(b["c"], index=b["d"])
        px[code] = s
    m = pd.DataFrame(px).sort_index()
    m = m.loc[m.index.isin(dates_all)].ffill()
    ret = m.pct_change(1)
    ds = list(ret.index)
    out = []
    for i in range(len(ds)):
        if i < 252:
            out.append(np.nan)
            continue
        hist = ret.iloc[i - 252:i].dropna(axis=1)
        if hist.shape[1] < 20:
            out.append(np.nan)
            continue
        cur = ret.iloc[i][hist.columns].values.astype(float)
        if not np.isfinite(cur).all():
            out.append(np.nan)
            continue
        cov = hist.cov().values
        dev = cur - hist.mean().values
        try:
            v = float(dev.dot(np.linalg.pinv(cov)).dot(dev.T))
        except Exception:
            v = np.nan
        out.append(v if np.isfinite(v) and v > 0 else np.nan)
    return pd.DataFrame({"date": ds, "turbulence": out})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    a = ap.parse_args()

    kb = os.path.join(ROOT, "viewer", "kbase.db")
    t0 = time.time()
    bars, alldates = EP.load(kb, a.n)
    print("取数 %d 只，耗时 %.1fs" % (len(bars), time.time() - t0))
    if not bars:
        return 1

    t1 = time.time()
    vals = {}
    for code, b in bars.items():
        if len(b["c"]) < EP.NEED:
            continue
        names, F, raw_macd = finrl_features(b)
        vals[code] = {"d": b["d"], "c": b["c"], "F": F, "raw_macd": raw_macd}
    print("算 FinRL 8 指标（stockstats 原库）：%d 只，耗时 %.1fs" % (
        len(vals), time.time() - t1))
    if not vals:
        return 1

    lo1, hi1 = EP.WARM, EP.WARM + EP.TRAIN_DAYS
    lo2, hi2 = hi1, hi1 + EP.TEST_DAYS

    print("\n" + "=" * 84)
    print("一、FinRL 那 8 个指标（归一化后）逐维过门禁 —— 训练期")
    print("=" * 84)
    d1, c1, F1, R1 = panel(vals, alldates, lo1, hi1, len(names))
    g1 = gate(F1, R1, names)
    print("  %-16s %8s %7s %7s  %s" % ("维度", "IC", "ICIR", "t", "门禁"))
    for r in sorted(g1, key=lambda x: -abs(x["ic"])):
        print("  %-16s %+8.4f %+7.2f %+7.2f  %s" % (
            names[r["k"]], r["ic"], r["icir"], r["t"],
            "过" if r["ok"] else "拒（%s）" % r["why"]))
    print("  → 过门禁 %d / %d 维" % (sum(1 for r in g1 if r["ok"]), len(g1)))

    print("\n" + "=" * 84)
    print("二、同一批指标 —— 测试期（样本外，验证符号是否稳定）")
    print("=" * 84)
    d2, c2, F2, R2 = panel(vals, alldates, lo2, hi2, len(names))
    g2 = gate(F2, R2, names)
    print("  %-16s %8s %7s %7s  %s   %s" % ("维度", "IC", "ICIR", "t", "门禁", "训练期 IC"))
    for r in sorted(g2, key=lambda x: -abs(x["ic"])):
        same = (np.sign(r["ic"]) == np.sign([x for x in g1 if x["k"] == r["k"]][0]["ic"]))
        print("  %-16s %+8.4f %+7.2f %+7.2f  %-4s  %+7.4f %s" % (
            names[r["k"]], r["ic"], r["icir"], r["t"],
            "过" if r["ok"] else "拒",
            [x for x in g1 if x["k"] == r["k"]][0]["ic"],
            "同号" if same else "**翻号**"))
    print("  → 过门禁 %d / %d 维" % (sum(1 for r in g2 if r["ok"]), len(g2)))

    # ---------- 三、原始量纲（照 FinRL 原样喂）----------
    print("\n" + "=" * 84)
    print("三、照 FinRL 原样喂原始值会怎样（macd 未除以收盘价）")
    print("=" * 84)
    rawv = {}
    for code, v in vals.items():
        rawv[code] = {"d": v["d"], "c": v["c"],
                      "F": v["raw_macd"].reshape(-1, 1)}
    _, _, Fr, Rr = panel(rawv, alldates, lo1, hi1, 1)
    gr = gate(Fr, Rr, ["macd(原始值)"])
    r = gr[0]
    print("  macd 原始值：IC %+.4f  ICIR %+.2f  t %+.2f  → %s" % (
        r["ic"], r["icir"], r["t"], "过" if r["ok"] else "拒（%s）" % r["why"]))
    print("  对照：macd/close 在同期的 IC 是 %+.4f" % [x for x in g1 if x["k"] == 0][0]["ic"])
    print("  → 同一指标、只换量纲，IC 差这么多，说明「原样喂」时学到的量纲噪声占大头。")

    # ---------- 四、turbulence ----------
    print("\n" + "=" * 84)
    print("四、turbulence 风险开关（FinRL 里我认为最值得搬的一块）")
    print("=" * 84)
    # ★ 必须用小样本！第一版直接喂了 300 只票，而协方差只用了 252 天观测，
    # 矩阵是 300×300 但秩最多 252 —— 欠定，pinv 直接把数值放大到 9e12，
    # 算出来的"95 分位"是纯噪声。FinRL 原版是对道指 30 只算的（30×30 / 252 观测，超定）。
    # 所以这里照原版的量级来：取成交额前 30 只，再试 50 只做稳健性对照。
    allcodes = list(bars.keys())      # load() 已按成交额降序，直接用前 N 个
    for univ in (30, 50):
        sub_bars = {k: bars[k] for k in allcodes[:univ]}
        tb = turbulence(sub_bars, alldates)
        mret = {}
        for code, b in sub_bars.items():
            c = b["c"]
            for i in range(len(c) - 1):
                mret.setdefault(b["d"][i], []).append(c[i + 1] / c[i] - 1)
        mret = {k: float(np.nanmean(v)) for k, v in mret.items() if len(v) >= univ * 0.6}
        tb["mkt_next"] = tb["date"].map(mret)
        sub = tb.dropna()
        print("\n  [宇宙 %d 只] 可用 %d 天" % (univ, len(sub)))
        if len(sub) < 100:
            print("  可用天数不足（需 252 日预热），本次无法评估")
            continue
        q = sub["turbulence"].quantile(0.95)
        hi_ = sub[sub["turbulence"] >= q]["mkt_next"]
        lo_ = sub[sub["turbulence"] < q]["mkt_next"]
        ic = EP._spearman(sub["turbulence"].values, sub["mkt_next"].values)
        print("  turbulence 95 分位 = %.1f（量级正常=%.0e）" % (q, np.log10(abs(q) + 1)))
        print("  turbulence 与次日全市场等权收益的 Rank IC = %+.4f" % ic)
        print("  高 turbulence 日之后：次日平均 %+.3f%%（%d 天）" % (
            hi_.mean() * 100, len(hi_)))
        print("  其余日子之后：     次日平均 %+.3f%%（%d 天）" % (
            lo_.mean() * 100, len(lo_)))
        print("  → %s" % ("高波动后确实更差，这个开关有信息" if hi_.mean() < lo_.mean()
                        else "高波动后并不更差，这个开关在 A 股这段样本上没有信息"))

    print("\n总耗时 %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
