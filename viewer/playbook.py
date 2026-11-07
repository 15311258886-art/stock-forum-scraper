# -*- coding: utf-8 -*-
"""判据库：把"方法"变成"可插拔的判据" —— 2026-11-05

## 为什么要有这个文件

之前每加一个方法，都要在 `toolbox.py` 里单独写一遍接线、单独测一次、单独写说明。
结果是：GitHub 上抄了 11 条就停了，自己沉淀的金叉、布林、缺口、连板**一条都没进来**。
不是不会写，是每次都当成一个"新工程"。

这里改成统一接口：**写 3 行判据，就自动进回测矩阵、自动定档、自动判正负。**

```python
PLAYBOOK["ma5_cross_ma20"] = dict(
    name="MA5 上穿 MA20（金叉）",
    role="选股", grp="pb_trend", h=(3, 5, 10, 20),
    src="技术分析常识",
    claimed="金叉后短期偏强（待验证）",
    fn=lambda X, p: _cross(X, "ma5", "ma20"),
)
```

## 接口约定

`fn(X, p) -> np.ndarray[bool]`，长度与 X 里所有数组一致（逐日）。
`X` 由 `enrich_X()` 填好，包含全部指标 + 原始价量。

## 为什么不在这个文件里算指标

指标必须与产品 `stock_indicators_daily` 逐位一致（`tools/audit_indicators.py` 已验到 0 差异）。
所以指标只从 `viewer.indicators.compute_indicators` 来，这里只做判据组合，绝不另算一遍。

## 怎么自动进回测

`tools/cond_engine.py` 会 `TOOLS.update(playbook.as_tools())`，
所以新判据写进来之后，跑一次 `python tools/cond_matrix.py` 就自动有了 405 格结果。
"""
from __future__ import annotations

import numpy as np

__all__ = ["PLAYBOOK", "as_tools", "enrich_X"]


def _sh(a: np.ndarray, k: int = 1) -> np.ndarray:
    """整体后移 k 位（前面补 NaN），用来判"上穿"。

    k=0 时直接返回副本 —— 不能走下面那个分支：`a[:-0]` 在 Python 里是空数组，
    会报 "could not broadcast input array from shape (0,)"。量价背离那边循环
    从 0 开始取窗口，正好踩到这个。
    """
    if k <= 0:
        return np.array(a, dtype=np.float64, copy=True)
    out = np.full_like(a, np.nan, dtype=np.float64)
    if k < len(a):
        out[k:] = a[:-k]
    return out


def _cross(X: dict, fast: str, slow: str) -> np.ndarray:
    f, s = X[fast], X[slow]
    pf, ps = _sh(f), _sh(s)
    return (f > s) & (pf <= ps) & np.isfinite(f) & np.isfinite(s)


def _limit_thr(code: str) -> float:
    c = str(code or "")
    if c[:3] in ("300", "301", "688", "689"):
        return 19.8
    if c[:1] in ("4", "8") or c[:3] == "920":
        return 29.8
    return 9.8


def enrich_X(X: dict, df, ind) -> None:
    """把判据库需要的字段补进 X（就地修改）。缺字段不报错，相关判据会返回全 False。"""
    def arr(k):
        try:
            return ind[k].to_numpy(np.float64)
        except Exception:      # noqa: BLE001
            return np.full(len(df), np.nan)

    X.setdefault("ma10", arr("ma10"))
    X.setdefault("macds", arr("macds"))
    X.setdefault("kdjk", arr("kdjk"))
    X.setdefault("kdjd", arr("kdjd"))
    X.setdefault("boll", arr("boll"))
    X.setdefault("boll_ub", arr("boll_ub"))
    X.setdefault("boll_lb", arr("boll_lb"))
    if "high" not in X:
        X["high"] = df["high"].to_numpy(np.float64)
    if "low" not in X:
        X["low"] = df["low"].to_numpy(np.float64)


def _f(name: str):
    """取字段；缺字段时返回全 NaN，判据自然全 False（不炸）。"""
    def get(X, p=None):
        return X.get(name)
    return get


def _nan_like(X: dict) -> np.ndarray:
    for v in X.values():
        if isinstance(v, np.ndarray):
            return np.full(v.shape, np.nan)
    return np.array([])


# ---------------------------------------------------------------- 判据清单
# 每一条都写清「来源」和「它自己声称适用什么」。声称对不对，由矩阵结果裁决。

def _ma5_cross_ma20(X, p):
    return _cross(X, "ma5", "ma20")


def _ma10_cross_ma60(X, p):
    return _cross(X, "ma10", "ma60")


def _macd_golden(X, p):
    return _cross(X, "macd", "macds")


def _kdj_golden(X, p):
    return _cross(X, "kdjk", "kdjd")


def _boll_break_ub(X, p):
    return np.isfinite(X["boll_ub"]) & (X["close"] > X["boll_ub"])


def _boll_above_mid(X, p):
    return _cross(X, "close", "boll")


def _ma_converge(X, p):
    """均线粘合：MA5/MA20/MA60 的极差 ≤3%（粘合后往往选方向）。"""
    a, b, c = X["ma5"], X["ma20"], X["ma60"]
    mx = np.maximum(np.maximum(a, b), c)
    mn = np.minimum(np.minimum(a, b), c)
    with np.errstate(divide="ignore", invalid="ignore"):
        spread = (mx - mn) / np.where(mn > 0, mn, np.nan) * 100.0
    return np.isfinite(spread) & (spread <= 3.0) & (mn > 0)


def _gap_up(X, p):
    """向上跳空：当日最低 > 前一日最高（缺口未回补无法用当日数据判断，只判跳空本身）。"""
    prev_high = _sh(X["high"])
    return np.isfinite(prev_high) & (X["low"] > prev_high)


def _vol_price_diverge(X, p):
    """量价背离：创 20 日新高，但量比 <0.8（价升量缩）。

    窗口取 k=1..20：与**前 20 日**比新高，不含今日自己（含今日自己等于恒真）。
    """
    c = X["close"]
    hi20 = np.full_like(c, np.nan, dtype=np.float64)
    for k in range(1, 21):
        sh = _sh(c, k)
        hi20 = np.where(np.isfinite(sh), np.fmax(hi20, sh), hi20)
    return (np.isfinite(hi20) & (c >= hi20) & (X["volume_ratio"] < 0.8))


def _limit_up_first(X, p):
    thr = X.get("_limit_thr")
    if thr is None:
        return np.zeros(len(X["close"]), dtype=bool)
    r, pr = X["ret1"], _sh(X["ret1"])
    return (r >= thr - 0.2) & np.isfinite(pr) & (pr < thr - 0.2)


def _limit_up_2(X, p):
    thr = X.get("_limit_thr")
    if thr is None:
        return np.zeros(len(X["close"]), dtype=bool)
    r, pr = X["ret1"], _sh(X["ret1"])
    return (r >= thr - 0.2) & np.isfinite(pr) & (pr >= thr - 0.2)


def _turnover_spike(X, p):
    return np.isfinite(X["volume_ratio"]) & (X["volume_ratio"] >= 3.0)


def _shrink_pull_ma20(X, p):
    """缩量回踩 MA20 上方：收盘 ≥ MA20，且量比 ≤0.8。
    注意与 oss_pullback_ma20（要求收盘在 MA20 **之下**）口径相反，不可互换。"""
    return (np.isfinite(X["ma20"]) & (X["close"] >= X["ma20"])
            & np.isfinite(X["volume_ratio"]) & (X["volume_ratio"] <= 0.8))


PLAYBOOK: dict[str, dict] = {
    # ---- 趋势类 ----
    "pb_ma5_cross_ma20": dict(
        name="MA5 上穿 MA20（金叉）", role="选股", grp="pb_trend", h=(3, 5, 10, 20),
        src="技术分析常识", claimed="金叉后短期偏强",
        note="金叉（MA5 上穿 MA20）", fn=_ma5_cross_ma20),
    "pb_ma10_cross_ma60": dict(
        name="MA10 上穿 MA60（中期金叉）", role="选股", grp="pb_trend", h=(10, 20, 60),
        src="技术分析常识", claimed="中期趋势转多",
        note="MA10 上穿 MA60", fn=_ma10_cross_ma60),
    "pb_macd_golden": dict(
        name="MACD 金叉（DIF 上穿 DEA）", role="选股", grp="pb_trend", h=(5, 10, 20),
        src="技术分析常识", claimed="动能转强",
        note="MACD 金叉", fn=_macd_golden),
    "pb_kdj_golden": dict(
        name="KDJ 金叉（K 上穿 D）", role="选股", grp="pb_trend", h=(3, 5, 10),
        src="技术分析常识", claimed="短线转强",
        note="KDJ 金叉", fn=_kdj_golden),
    "pb_boll_break_ub": dict(
        name="突破布林上轨", role="选股", grp="pb_trend", h=(3, 5, 10, 20),
        src="技术分析常识", claimed="强势突破",
        note="收盘站上布林上轨", fn=_boll_break_ub),
    "pb_boll_above_mid": dict(
        name="上穿布林中轨", role="选股", grp="pb_trend", h=(3, 10, 20),
        src="技术分析常识", claimed="由弱转强",
        note="收盘上穿布林中轨", fn=_boll_above_mid),
    "pb_ma_converge": dict(
        name="均线粘合（MA5/20/60 极差≤3%）", role="选股", grp="pb_trend", h=(5, 10, 20),
        src="进哥提过的「粘合」形态", claimed="粘合后选方向，向上概率待验证",
        note="均线粘合", fn=_ma_converge),
    # ---- 形态类 ----
    "pb_gap_up": dict(
        name="向上跳空缺口", role="选股", grp="pb_form", h=(3, 5, 10, 20),
        src="技术分析常识", claimed="缺口不回补则强",
        note="当日最低 > 前日最高", fn=_gap_up),
    "pb_vol_price_diverge": dict(
        name="量价背离（新高缩量）", role="排除", grp="pb_form", h=(3, 5, 10, 20),
        src="技术分析常识", claimed="价升量缩是隐患",
        note="创 20 日新高但量比<0.8", fn=_vol_price_diverge),
    "pb_shrink_pull_ma20": dict(
        name="缩量站稳 MA20", role="选股", grp="pb_form", h=(3, 5, 10, 20),
        src="技术分析常识", claimed="缩量回踩不破",
        note="收盘≥MA20 且量比≤0.8（与 oss_pullback_ma20 口径相反）",
        fn=_shrink_pull_ma20),
    # ---- 情绪类 ----
    "pb_limit_up_first": dict(
        name="首板（当日涨停且前日未涨停）", role="选股", grp="pb_sent", h=(1, 3, 5, 10),
        src="A 股打板常识", claimed="首板后有惯性",
        note="首板", fn=_limit_up_first),
    "pb_limit_up_2": dict(
        name="二连板（连续两日涨停）", role="选股", grp="pb_sent", h=(1, 3, 5),
        src="A 股打板常识", claimed="连板更强",
        note="二连板", fn=_limit_up_2),
    "pb_turnover_spike": dict(
        name="量比≥3（换手突变）", role="选股", grp="pb_sent", h=(1, 3, 5, 10),
        src="量价常识", claimed="异动",
        note="量比≥3", fn=_turnover_spike),
}


def as_tools() -> dict[str, dict]:
    """转成 `cond_engine.TOOLS` 的格式，供矩阵直接调用。"""
    out = {}
    for key, spec in PLAYBOOK.items():
        # 引擎按 fn(X) 单参调用，判据库函数统一是 fn(X, p)。这里包一层把 p 填成空 dict，
        # 不能直接把 spec["fn"] 塞进去 —— 会报 missing 1 required positional argument: 'p'。
        # 默认参数用 _f=... 固定住，否则闭包会全部指向最后一个 spec["fn"]。
        out[key] = dict(fn=(lambda X, _f=spec["fn"]: _f(X, {})),
                        h=spec["h"], grp=spec["grp"],
                        note="%s ｜ 来源：%s ｜ 声称：%s"
                             % (spec["note"], spec["src"], spec["claimed"]))
    return out
