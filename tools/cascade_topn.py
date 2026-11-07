"""Step 5（前 N 只口径，2026-11-05）：剔除器对「我实际下单的那 5~10 只」到底有没有用。

为什么要有这一步（换成前 N 只而不是池子均值）
------------------------------------------------
前面 Step 1~4 的 `Δ_rule` 衡量的是**池子均值**。老板关心的不是池子均值，是
「今天这 5 只赚不赚」。池子 5000 只的期望从 −0.250% 挪到 −0.241%，对下单毫无意义；
但如果剔除器能让他真正买到的那 10 只里少踩一个雷，那才叫有用。所以这里把主指标
换成：

    Δw_N = 前 N 只的胜率差（先剔后取前 N  −  不剔直接取前 N）
    Δm_N = 前 N 只的超额差（同样的两个集合）

池子均值只留作附注。

三个必须一起报的数（少一个就是自欺）
------------------------------------
1. **Δw_N / Δm_N**：主指标，逐日算再聚合（每天真下单 N 只，日权重才是用户视角）。
2. **误杀率**：被剔除的票里有多大比例在后一持有期**跑赢市场**。剔除器如果只是
   随机赶人，误杀率会等于市场基准；如果高于基准，说明它把好票也赶走了。
3. **换手代价**：先剔后取前 N，会让每天的买入名单换掉几只。为了 1bp 的改善每天
   多换 2 只票（来回 0.5% 成本），是净亏 —— 这一列不算，前面的 Δ 全是帐面数字。

口径（全部固定在这里，跑之前就定死）
------------------------------------
· 信号在 d 收盘，d+1 收盘买入，d+1+H 收盘卖出，扣 0.25% 往返。
· 收益用**前复权**（`data/kline_daily.db`），与 Step 6 主版本一致；
  桶成员用 `viewer/factors.py` 的**生产代码原样**逐日重算（不另写一套判据）。
· 基本面/情绪类桶（t1_surf / mid_base / long_hold）**无法重建**：
  需要当日声量、资金流、竞价快照、政策文本、季报批次，库里没有历史快照。
  这两类不测，理由写在报告里，不拿代理变量凑数。

为什么桶成员要自己重算
----------------------
`pillar_item`（支柱每日 4~5 只）只有 2026-10-14 起的 16 天，`sig_unified` 只有 20 天，
`stock_strategy_picks.updated` 全是空串。**没有任何 261 天的历史命中表**。
唯一能忠实还原的，是 `factors.py` 里那 6 个纯函数（只吃 OHLCV，判据在代码里写死）。
所以这里重建的是「桶的漏斗」而不是「桶的最终名单」：
  · 超跌修复：`tv_shrink_pull` ∩ `db_chan`（缩量回踩 + 缠论中枢下沿）
  · 中线突破：`tv_wbottom` ∩ `db_wave`（双底 + 波浪三浪启动）
交集口径＝`run_funnel` 的语义（工具是步骤，步骤是 AND）。

被排除的工具与理由（**不是挑好看的，是交集恒空**）
--------------------------------------------------
· `tv_platform`（平台突破）：一天只出 0~18 只，多数日子 0 只，与任何工具求交恒空。
· `tv_macd_div`（MACD 背离）：与 `tv_shrink_pull` 的交集在抽样 6 天里 5 天为 0
  （0/0/5/0/0/1），三工具交集 6/6 天为 0。
放进漏斗等于宣布「这个桶永远没有票」，所以按机械规则排除：
**与其它工具在过半交易日无交集的工具，不进漏斗**。

运行
----
    .venv/Scripts/python.exe tools/cascade_topn.py --stage pool     # 只建桶成员，落盘
    .venv/Scripts/python.exe tools/cascade_topn.py --stage run      # 出表
    .venv/Scripts/python.exe tools/cascade_topn.py --stage all
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))          # `viewer.factors` / `viewer.universe_box` 在这里

import numpy as np                                            # noqa: E402

import backtest_oss as B                                      # noqa: E402  同口径唯一来源
from cascade_delta import QFQ_SPEC, RULE_CN                    # noqa: E402

LOG_DIR = ROOT / "viewer" / "logs"
DOC_DIR = ROOT / "docs"
STAMP = "20261105"

NPZ_QFQ = LOG_DIR / ("cascade_matrix_qfq_%s.npz" % STAMP)
POOL_NPZ = LOG_DIR / ("cascade_topn_pool_%s.npz" % STAMP)
OUT_JSON = LOG_DIR / ("cascade_topn_%s.json" % STAMP)

# ---- 口径常量（跑之前定死） ----
WINDOW_DAYS = 261            # 团队要求：261 个信号日
N_LIST = (5, 10, 20)         # 前 N 只
H_LIST = (1, 2, 3, 5, 10, 20)   # 只报 T+1~T+20
DRAWS = 50                   # 随机对照抽样次数
COST = B.COST_PCT            # 0.25
FACTOR_KEEP = 320            # 与 viewer/factors.LOAD_KEEP 一致（生产就是 320 根）
LOAD_KEEP = 581              # 窗口 261 天 + 320 根预热
MIN_POOL = 2                 # 池子少于 2 只当日不作数

# ---- 判定门槛（团队新给的，比 t 值更硬） ----
ECON_OK = 0.25               # ≥0.25pp（赚回一次往返成本）→ 可用
ECON_INFO = 0.05             # 0.05~0.25pp → 有信息但不可用
WR_CLOSE = 0.5               # 前 N 只 Δw_N < 0.5pp → 这条线关掉
T_CLOSE = 2.0

BUCKETS: dict[str, dict] = {
    "short_dip": {
        "cn": "超跌修复（生产桶 dip_revert 的技术面漏斗）",
        "tools": ("f_shrink_pull", "f_chan"),
        "tool_cn": {"f_shrink_pull": "缩量回踩 MA20", "f_chan": "缠论中枢下沿"},
        "excluded": {"f_platform": "当日 0~18 只，交集恒空",
                     "f_macd_div": "与缩量回踩交集 6 天里 5 天为 0，三工具交集 6/6 天为 0"},
        "rules": ("oss_pullback_ma20", "oss_mom20", "oss_reversal20"),
    },
    "mid_break": {
        "cn": "中线突破（双底 + 波浪三浪启动）",
        "tools": ("f_wbottom", "f_wave"),
        "tool_cn": {"f_wbottom": "双底 W", "f_wave": "波浪三浪启动"},
        "excluded": {"f_platform": "当日 0~18 只，交集恒空"},
        "rules": ("oss_ma_align",),
    },
}
# 闸门规则不专属某个桶，两个桶都测一遍
GATE_RULES = ("oss_high_vol",)


# ================================================================
# ① 桶成员：用生产代码逐日重算
# ================================================================

def load_factor_bars() -> dict:
    """取日线 → {code: dict(arr..., d=[日期])}，字段口径与 `factors.load` 完全一致。

    为什么不用 `factors.load`：它**故意把日期丢掉**（因子本身不用日期）。这里要在
    261 个历史交易日上切片重放，必须把日期留下。列序转换仍然走
    `universe_box.load_bars`（它已经把 (d,o,h,l,c,v) 转成 (d,o,c,h,l,v)），不自己转。
    """
    from viewer import universe_box as ub

    t0 = time.time()
    raw = ub.load_bars(None, keep=LOAD_KEEP)
    out = {}
    for code, rows in raw.items():
        if not rows:
            continue
        a = np.asarray([r[1:6] for r in rows], dtype=np.float64)
        out[str(code)] = {
            "n": len(rows), "o": a[:, 0], "c": a[:, 1],
            "h": a[:, 2], "l": a[:, 3], "v": a[:, 4],
            "d": [r[0] for r in rows],
            "pos": {d: i for i, d in enumerate(r[0] for r in rows)},
        }
    print("[pool] 取数 %d 只（%.1fs）" % (len(out), time.time() - t0), flush=True)
    return out


def build_pool(codes: np.ndarray, dates: np.ndarray) -> dict:
    """对每个信号日，跑一遍桶里的生产因子，求交 → (是否在池, 排序分数)。

    `dates` 来自 qfq 矩阵（信号日），`codes` 是它的票序；成员矩阵按这个顺序对齐。
    分数取「命中该票的所有因子的平均分」（都在 0~100 同一量纲，不另做标准化 ——
    标准化本身就是一个可调参数，加了就说不清是因子在排序还是我在排序）。
    """
    from viewer import factors as fa

    bars = load_factor_bars()
    n_stock, n_day = len(codes), len(dates)
    # 工具 -> (命中, 分数)。一个工具只跑一次，两个桶共用 `f_shrink_pull` 这类重复项。
    tools = sorted({t for s in BUCKETS.values() for t in s["tools"]})
    ok_all = {t: np.zeros((n_stock, n_day), dtype=bool) for t in tools}
    sc_all = {t: np.zeros((n_stock, n_day), dtype=np.float32) for t in tools}

    t0 = time.time()
    for i, code in enumerate(codes):
        b = bars.get(code)
        if b is None:
            continue
        if i % 500 == 0:
            print("      票 %d/%d（%.0fs）" % (i, n_stock, time.time() - t0), flush=True)
        o, c, h, l, v, pos = b["o"], b["c"], b["h"], b["l"], b["v"], b["pos"]
        for j, d in enumerate(dates):
            idx = pos.get(d)
            if idx is None or idx + 1 < FACTOR_KEEP:
                continue
            sl = slice(idx + 1 - FACTOR_KEEP, idx + 1)
            sub = {"n": FACTOR_KEEP, "o": o[sl], "c": c[sl],
                   "h": h[sl], "l": l[sl], "v": v[sl]}
            for t in tools:
                ok, sc, _ = getattr(fa, t)(sub, {})
                if ok:
                    ok_all[t][i, j] = True
                    sc_all[t][i, j] = float(sc or 0.0)

    store = {"codes": codes, "dates": dates}
    for t in tools:
        store["tool_" + t] = ok_all[t]
    for bkey, spec in BUCKETS.items():
        p = np.ones((n_stock, n_day), dtype=bool)
        acc = np.zeros((n_stock, n_day), dtype=np.float32)
        for t in spec["tools"]:
            p &= ok_all[t]                       # 交集＝run_funnel 的语义（步骤是 AND）
            acc += sc_all[t]
        store["pool_" + bkey] = p
        # 分数＝命中该票的各因子均分（同一量纲 0~100，不再二次标准化）
        store["score_" + bkey] = np.where(
            p, acc / max(1, len(spec["tools"])), 0.0).astype(np.float32)
    print("[pool] 成员矩阵完成（%.1fs）" % (time.time() - t0), flush=True)
    return store


def stage_pool() -> dict:
    z = np.load(NPZ_QFQ, allow_pickle=False)
    codes, dates = z["codes"], z["dates"]
    win = np.where((dates >= "2025-09-30") & (dates <= "2026-11-03"))[0]
    if win.size != WINDOW_DAYS:
        print("[pool] 注意：窗口 %d 天（期望 %d）" % (win.size, WINDOW_DAYS), flush=True)
    # 只对窗口内的日子建成员；矩阵仍按全票 × 全窗对齐（列＝窗口内信号日）
    sub_dates = dates[win]
    store = build_pool(codes, sub_dates)
    store["cols_full"] = win
    np.savez(POOL_NPZ, **store)
    meta = {
        "stage": "pool",
        "window_days": int(sub_dates.size),
        "first": str(sub_dates[0]), "last": str(sub_dates[-1]),
        "n_stocks": int(codes.size),
        "npz": str(POOL_NPZ),
        "npz_mb": round(POOL_NPZ.stat().st_size / 1048576.0, 1),
    }
    for bkey in BUCKETS:
        p = store["pool_" + bkey]
        cnt = p.sum(axis=0)
        meta.setdefault("pool_size", {})[bkey] = {
            "min": int(cnt.min()), "p25": float(np.percentile(cnt, 25)),
            "p50": float(np.median(cnt)), "p75": float(np.percentile(cnt, 75)),
            "max": int(cnt.max()), "mean": round(float(cnt.mean()), 1),
        }
    return meta


# ================================================================
# ② 收益：前复权逐日重算，并与矩阵缓存对账
# ================================================================

def build_returns(codes: np.ndarray, dates: np.ndarray,
                  warm: int = B.WARMUP_BARS, h_list=None) -> dict:
    """算 ret_H / bench_H / exc_H，取值口径与 `cascade_delta.build_matrix` 逐字一致。

    预热长度必须跟 `build_matrix` 一样（`WARMUP_BARS`=300 根），不能拍脑袋写个日期：
    平仓端停牌是**用前值顺延**的（`maximum.accumulate`），如果 cal 从窗口中间开始，
    一只票如果恰好跨窗口边界停牌，它会被顺延成 NaN 而不是窗口前的那个价 ——
    差一天就足以让「与主矩阵对账」从 0.02pp 变成几百个格子对不上。
    """
    n_stock = codes.size
    n_day = dates.size
    d1 = str(dates[-1])
    cal_all = QFQ_SPEC.calendar("1990-01-01", d1)
    i0 = cal_all.index(str(dates[0]))
    cal = cal_all[max(0, i0 - warm):]
    cal_map = {d: i for i, d in enumerate(cal)}
    n_cal = len(cal)
    code_pos = {str(c): i for i, c in enumerate(codes)}

    mat = np.full((n_stock, n_cal), np.nan, dtype=np.float32)
    for code, cb, _hb, _lb, _vb, db in QFQ_SPEC.iter_bars(cal[0], d1):
        i = code_pos.get(str(code))
        if i is None:
            continue
        gidx = np.array([cal_map.get(d, -1) for d in db], dtype=np.int64)
        keep = gidx >= 0
        if keep.sum() < B.MIN_LISTED_BARS:
            continue
        mat[i, gidx[keep]] = np.asarray(cb, dtype=np.float64)[keep].astype(np.float32)

    present = np.isfinite(mat)
    idx = np.where(present, np.arange(n_cal)[None, :], 0)
    np.maximum.accumulate(idx, axis=1, out=idx)
    filled = np.take_along_axis(mat, idx, axis=1)

    z = np.load(NPZ_QFQ, allow_pickle=False)
    uni_full = z["uni"]
    cols_full = np.array([list(z["dates"]).index(d) for d in dates])
    uni = uni_full[:, cols_full]

    out = {}
    for H in (h_list or H_LIST):
        ts = np.array([cal_map[d] for d in dates])
        ok = ts + 1 + H < n_cal
        ret = np.full((n_stock, n_day), np.nan, dtype=np.float32)
        entry = mat[:, ts[ok] + 1]
        exitp = filled[:, ts[ok] + 1 + H]
        with np.errstate(invalid="ignore", divide="ignore"):
            r = (exitp / entry - 1.0) * 100.0
        r = np.where(np.isfinite(r), r, np.nan).astype(np.float32)
        base = uni[:, ok] & np.isfinite(r)
        ret[:, ok] = r
        cnt = base.sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            bench = np.where(cnt > 0,
                             np.where(base, r, 0.0).sum(axis=0) / np.maximum(cnt, 1), np.nan)
        exc_l = np.where(base, r - bench[None, :] - COST, np.nan).astype(np.float32)
        exc = np.full((n_stock, n_day), np.nan, dtype=np.float32)
        exc[:, ok] = exc_l
        benchf = np.full(n_day, np.nan, dtype=np.float32)
        benchf[ok] = bench.astype(np.float32)
        out["ret_%d" % H] = ret
        out["exc_%d" % H] = exc
        out["bench_%d" % H] = benchf
        out["ok_%d" % H] = np.ones(n_day, dtype=bool) * False
        out["ok_%d" % H][ok] = True
        # 对账：与矩阵缓存里的 exc 比（同一公式，应当逐格一致到 float32 量级）
        ref = z["exc_%d" % H][:, cols_full]
        cmp = np.isfinite(exc) & np.isfinite(ref)
        if cmp.sum():
            dmax = float(np.nanmax(np.abs(exc[cmp] - ref[cmp])))
            out.setdefault("_check", {})["H%d" % H] = {
                "cells": int(cmp.sum()), "max_abs_diff_pp": round(dmax, 6)}
    return out


# ================================================================
# ③ 前 N 只的三臂
# ================================================================

def nw_t(x: np.ndarray, lag: int) -> float:
    """Newey-West t（Bartlett 核）。H−1 期重叠必须用它，普通 t 会虚高。"""
    x = x[np.isfinite(x)]
    n = x.size
    if n < 10:
        return float("nan")
    xm = x - x.mean()
    g0 = float((xm * xm).sum()) / n
    var = g0
    for L in range(1, min(lag, n - 1) + 1):
        w = 1.0 - L / (lag + 1.0)
        gl = float((xm[L:] * xm[:-L]).sum()) / n
        var += 2.0 * w * gl
    se = np.sqrt(max(var, 0.0) / n)
    return float(x.mean() / se) if se > 0 else float("nan")


def _econ(d: float) -> str:
    if not np.isfinite(d):
        return "—"
    a = abs(d)
    if a >= ECON_OK:
        return "可用"
    if a >= ECON_INFO:
        return "有信息但不可用"
    return "无经济意义"


def topn_metrics(pool: np.ndarray, score: np.ndarray, hit: np.ndarray,
                 ret: np.ndarray, exc: np.ndarray, N: int, H: int,
                 rng: np.random.Generator) -> dict:
    """单条 (规则, 桶, N, H) 的前 N 只三臂。

    主指标按**日**算再取日均（每天真买 N 只，日权重才对应用户体验）。
    另外报 pool 加权版，两者差异大就说明结论靠少数几天撑着。
    """
    n_stock, n_day = pool.shape
    keys = ("w_base", "w_filt", "w_ctrl", "w_ctrl2", "m_base", "m_filt", "m_ctrl", "m_ctrl2",
            "wa_base", "wa_filt", "chg", "k", "poolsz")
    acc: dict[str, list] = {k: [] for k in keys}
    n_empty = n_short = n_idle = n_thin = 0
    kill = []          # 被剔掉的票（超额>0 比例）
    kill_top = []      # 被剔掉、且本来在前 N 里的票
    for j in range(n_day):
        cand = pool[:, j] & np.isfinite(exc[:, j])
        n_pool = int(cand.sum())
        if n_pool < MIN_POOL:
            n_empty += 1
            continue
        if n_pool < N:
            n_short += 1              # 池子连 N 只都凑不满 → N 这一档不成立
            continue
        cols = np.where(cand)[0]
        order = cols[np.argsort(-score[cols, j], kind="stable")]
        top_base = order[:N]
        drop = cand & hit[:, j]
        k = int(drop.sum())
        if k == 0:
            n_idle += 1               # 空转日：剔除器今天一只也没碰到 → 名单不变，Δ=0
            continue
        keep = cand & ~hit[:, j]
        if keep.sum() < N:
            n_thin += 1               # 剔完凑不满 N 只 → 不可比（换手率会虚高）
            continue
        kcols = np.where(keep)[0]
        korder = kcols[np.argsort(-score[kcols, j], kind="stable")]
        top_filt = korder[:N]

        # 对照臂 A（团队原话口径）：从整个池子里随机剔 k 只，再取前 N
        ctrl_w, ctrl_m = [], []
        for _ in range(DRAWS):
            rm = rng.choice(cols, size=min(k, cols.size), replace=False)
            left = order[~np.isin(order, rm)]
            if left.size == 0:
                continue
            e = exc[left[:N], j]
            ctrl_w.append(float((e > 0).mean()))
            ctrl_m.append(float(e.mean()))

        # 对照臂 B（同工同酬口径）：随机换掉同样多的**前 N 席位**，后面按排序补位。
        # 为什么要有这一臂：A 臂随机剔 k 只，落进前 N 的期望只有 k·N/|pool| 席；
        # 而规则剔除往往精准打在前 N 里。两臂换掉的席位数不同，Δ 就不可比 ——
        # 拿 A 臂当唯一对照，等于让规则自带「换手越多越容易碰上运气」的优势。
        #
        # ⚠ 分母纪律：n_swap==0 的日子必须**照样记一次 Δ=0**，不能跳过。
        # 第一版跳过了，结果 B 臂变成「只在换过席位的日子上的均值」，被系统性放大 ——
        # 而 Δw 是含空转日的日均值，两个分母不一样，比出来的差值没有意义。
        n_swap = int(N - np.intersect1d(top_base, top_filt).size)
        ctrl2_w, ctrl2_m = [], []
        if n_swap > 0:
            pool_rest = order[N:]
            for _ in range(DRAWS):
                if pool_rest.size < n_swap:
                    break
                drop_slot = rng.choice(top_base, size=n_swap, replace=False)
                fill = rng.choice(pool_rest, size=n_swap, replace=False)
                sel = np.concatenate([top_base[~np.isin(top_base, drop_slot)], fill])
                e = exc[sel, j]
                ctrl2_w.append(float((e > 0).mean()))
                ctrl2_m.append(float(e.mean()))
        eb0 = exc[top_base, j]
        w2 = float(np.mean(ctrl2_w)) if ctrl2_w else float((eb0 > 0).mean())
        m2 = float(np.mean(ctrl2_m)) if ctrl2_m else float(eb0.mean())

        eb, ef = exc[top_base, j], exc[top_filt, j]
        acc["w_base"].append(float((eb > 0).mean()))
        acc["w_filt"].append(float((ef > 0).mean()))
        acc["w_ctrl"].append(float(np.mean(ctrl_w)) if ctrl_w else np.nan)
        acc["w_ctrl2"].append(w2)
        acc["m_base"].append(float(eb.mean()))
        acc["m_filt"].append(float(ef.mean()))
        acc["m_ctrl"].append(float(np.mean(ctrl_m)) if ctrl_m else np.nan)
        acc["m_ctrl2"].append(m2)
        acc["wa_base"].append(float((ret[top_base, j] > 0).mean()))
        acc["wa_filt"].append(float((ret[top_filt, j] > 0).mean()))
        acc["chg"].append(int(n_swap))
        acc["k"].append(k)
        acc["poolsz"].append(n_pool)
        ed = exc[np.where(drop)[0], j]
        kill.append(float((ed > 0).mean()))
        hit_top = np.intersect1d(top_base, np.where(drop)[0])
        if hit_top.size:
            kill_top.append(float((exc[hit_top, j] > 0).mean()))

    n_all = n_empty + n_short + n_idle + n_thin + len(acc["w_base"])
    live = n_idle + n_thin + len(acc["w_base"])     # 池子够 N 只的日子
    out = {
        "n_window": int(n_all), "n_pool_lt2": int(n_empty), "n_pool_ltN": int(n_short),
        "n_idle": int(n_idle), "n_thin": int(n_thin), "n_cmp": int(len(acc["w_base"])),
        "idle_pct": round(100.0 * n_idle / max(1, live), 1),
        "cmp_pct": round(100.0 * len(acc["w_base"]) / max(1, n_all), 1),
    }
    if not acc["w_base"]:
        out["note"] = "无可比日"
        return out
    a = {k: np.asarray(v, dtype=np.float64) for k, v in acc.items()}
    d_w = a["w_filt"] - a["w_base"]
    d_m = a["m_filt"] - a["m_base"]
    d_wc = a["w_ctrl"] - a["w_base"]
    d_wc2 = a["w_ctrl2"] - a["w_base"]
    d_mc2 = a["m_ctrl2"] - a["m_base"]
    days_share = len(acc["w_base"]) / max(1, n_all)
    out.update({
        "pool_p50": float(np.median(a["poolsz"])),
        "w_base": round(float(a["w_base"].mean()) * 100, 2),
        "w_filt": round(float(a["w_filt"].mean()) * 100, 2),
        "w_ctrl": round(float(np.nanmean(a["w_ctrl"])) * 100, 2),
        "w_ctrl2": round(float(np.nanmean(a["w_ctrl2"])) * 100, 2),
        "d_w": round(float(d_w.mean()) * 100, 3),
        "d_w_t": round(nw_t(d_w, H), 2),
        "d_wc": round(float(np.nanmean(d_wc)) * 100, 3),
        "d_wc2": round(float(np.nanmean(d_wc2)) * 100, 3),
        "d_wc2_t": round(nw_t(d_wc2, H), 2),
        # 全窗口折算：空转日/不满 N 的日子 Δ=0，这一列才是「每天下单」的真实改善
        "d_w_daily": round(float(d_w.mean()) * 100 * days_share, 4),
        "m_base": round(float(a["m_base"].mean()), 3),
        "m_filt": round(float(a["m_filt"].mean()), 3),
        "m_ctrl": round(float(np.nanmean(a["m_ctrl"])), 3),
        "m_ctrl2": round(float(np.nanmean(a["m_ctrl2"])), 3),
        "d_m": round(float(d_m.mean()), 3),
        "d_m_t": round(nw_t(d_m, H), 2),
        "d_mc": round(float(np.nanmean(a["m_ctrl"] - a["m_base"])), 3),
        "d_mc2": round(float(np.nanmean(d_mc2)), 3),
        "d_mc2_t": round(nw_t(d_mc2, H), 2),
        "d_m_daily": round(float(d_m.mean()) * days_share, 4),
        "wa_base": round(float(a["wa_base"].mean()) * 100, 2),
        "wa_filt": round(float(a["wa_filt"].mean()) * 100, 2),
        "d_wa": round(float((a["wa_filt"] - a["wa_base"]).mean()) * 100, 3),
        "chg": round(float(a["chg"].mean()), 2),
        "chg_pct": round(float(a["chg"].mean()) / N * 100, 1),
        "k": round(float(a["k"].mean()), 2),
        "poolsz": round(float(a["poolsz"].mean()), 1),
        "kill_rate": round(float(np.mean(kill)) * 100, 2) if kill else None,
        "kill_top_rate": round(float(np.mean(kill_top)) * 100, 2) if kill_top else None,
        "econ": _econ(float(d_m.mean())),
    })
    return out


def stage_run() -> dict:
    z = np.load(NPZ_QFQ, allow_pickle=False)
    codes = z["codes"]
    p = np.load(POOL_NPZ, allow_pickle=False)
    dates = p["dates"]
    cols_full = p["cols_full"]
    R = build_returns(codes, dates)

    # 市场基准胜率（全市场等权，扣成本后的「超额>0」比例应≈50%，当作 sanity）
    base_win = {}
    for H in H_LIST:
        e = R["exc_%d" % H][:, R["ok_%d" % H]]
        base_win[H] = round(float(np.nanmean(np.where(np.isfinite(e), e > 0, np.nan))) * 100, 2)

    rng = np.random.default_rng(20261105)
    rows = []
    for bkey, spec in BUCKETS.items():
        pool = p["pool_" + bkey]
        score = p["score_" + bkey]
        for rule in tuple(spec["rules"]) + GATE_RULES:
            hit = z["hit_" + rule][:, cols_full]
            for H in H_LIST:
                for N in N_LIST:
                    m = topn_metrics(pool, score, hit, R["ret_%d" % H], R["exc_%d" % H],
                                     N, H, rng)
                    if not m.get("n_cmp"):
                        continue
                    rows.append({"bucket": bkey, "bucket_cn": spec["cn"],
                                 "rule": rule, "rule_cn": RULE_CN.get(rule, rule),
                                 "N": N, "H": H, **m})
    meta = {
        "stage": "topn", "stamp": STAMP,
        "window": {"first": str(dates[0]), "last": str(dates[-1]), "days": int(dates.size)},
        "cost_pct": COST, "draws": DRAWS,
        "thresholds": {"econ_ok": ECON_OK, "econ_info": ECON_INFO,
                       "wr_close": WR_CLOSE, "t_close": T_CLOSE},
        "market_base_win": base_win,
        "returns_check": R.get("_check", {}),
        "pool_meta": {},
        "rows": rows,
    }
    for b, s in BUCKETS.items():
        cnt = p["pool_" + b].sum(axis=0)
        meta["pool_meta"][b] = {
            "tools": list(s["tools"]),
            "excluded": s["excluded"],
            "size_p25": float(np.percentile(cnt, 25)),
            "size_p50": float(np.median(cnt)),
            "size_p75": float(np.percentile(cnt, 75)),
            "size_max": int(cnt.max()),
            "days_nonzero": int((cnt > 0).sum()),
        }
    # 每条规则与每个桶的交集（股票日总数）—— 用来区分「效应小」和「根本碰不上」
    overlap = {}
    for b, s in BUCKETS.items():
        overlap[b] = {}
        pool = p["pool_" + b]
        for rule in tuple(s["rules"]) + GATE_RULES:
            hit = z["hit_" + rule][:, cols_full]
            k = (pool & hit).sum(axis=0)
            overlap[b][rule] = int(k.sum())
    meta["overlap"] = overlap
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[run] %d 行 → %s" % (len(rows), OUT_JSON), flush=True)
    return meta


def stage_check() -> int:
    """自检：先用构造出来的「假规则」验证指标函数本身会不会说谎。

    三件事各自能被证伪：
      [1] 收益重算与主矩阵逐格对账（口径没分叉）
      [2] 假规则 = 精准剔掉前 N 里最差的票 → Δw_N 必须显著为正
      [3] 假规则 = 随机剔同样多 → Δ 必须落在 0 附近（否则对照臂写错了）
      [4] 假规则 = 精准剔掉前 N 里最好的票 → Δw_N 必须显著为负
    跑不过任意一条，上面所有表都不能读。
    """
    z = np.load(NPZ_QFQ, allow_pickle=False)
    p = np.load(POOL_NPZ, allow_pickle=False)
    codes, dates, cols_full = z["codes"], p["dates"], p["cols_full"]
    R = build_returns(codes, dates)
    ok = True

    print("[1] 收益对账（与 cascade_matrix_qfq 逐格比）")
    for H in H_LIST:
        ck = R["_check"]["H%d" % H]
        flag = ck["max_abs_diff_pp"] < 0.05
        ok &= flag
        print("    T+%-2d 单元 %9d 最大偏差 %.4fpp  %s"
              % (H, ck["cells"], ck["max_abs_diff_pp"], "通过" if flag else "**失败**"))

    pool = p["pool_mid_break"]
    score = p["score_mid_break"]
    exc = R["exc_20"]
    N = 5
    H = 20
    rng = np.random.default_rng(7)

    def fake_rule(kind: str) -> np.ndarray:
        """造一条假规则：按当日超额排序去命中，用来验证指标函数的符号。"""
        hit = np.zeros_like(pool)
        for j in range(pool.shape[1]):
            cand = np.where(pool[:, j] & np.isfinite(exc[:, j]))[0]
            if cand.size < N:
                continue
            e = exc[cand, j]
            order = cand[np.argsort(e)]           # 超额从差到好
            if kind == "worst":
                hit[order[:N], j] = True          # 剔掉最差的 N 只
            elif kind == "best":
                hit[order[-N:], j] = True         # 剔掉最好的 N 只
            else:
                hit[rng.choice(cand, size=min(3, cand.size), replace=False), j] = True
        return hit

    for kind, want in (("worst", +1), ("best", -1)):
        m = topn_metrics(pool, score, fake_rule(kind), R["ret_%d" % H], exc, N, H,
                         np.random.default_rng(11))
        sign_ok = (m["d_w"] * want) > 0.5
        ok &= sign_ok
        print("[%s] 假规则「剔掉前 N 里最%s的票」→ Δw %+.2fpp（t %s）%s"
              % ("2" if kind == "worst" else "4", "差" if kind == "worst" else "好",
                 m["d_w"], m["d_w_t"], "通过" if sign_ok else "**失败**"))

    # [3] 零假设：随机剔 3 只的**单次**抽样噪声很大（f≈0.4 个席位/日）。
    #     注意这里**不能**要求 Δw 均值 ≈ 0 —— 实测它系统性为正（+1.2pp），
    #     原因不是指标错了，而是这个桶自己的打分在头部是反的（见下面 informational 一行）：
    #     换掉任何一个头部席位都平均占便宜。所以正确的零检验是
    #     「随机规则 ≈ 同槽随机对照」，即数据里不含除「换席位」以外的信息。
    draws, gaps = [], []
    for k in range(20):
        m = topn_metrics(pool, score, fake_rule("rand"), R["ret_%d" % H], exc, N, H,
                         np.random.default_rng(100 + k))
        draws.append(m["d_w"])
        if m.get("d_wc2") is not None:
            gaps.append(m["d_w"] - m["d_wc2"])
    mean_null = float(np.mean(draws))
    sd_null = float(np.std(draws, ddof=1))
    gap = float(np.mean(gaps)) if gaps else float("nan")
    null_ok = abs(gap) < 0.5
    ok &= null_ok
    print("[3] 假规则「随机剔 3 只」×20 条 → Δw 均值 %+.2fpp（单条 sd %.2fpp）；"
          "与同槽对照之差 %+.2fpp %s"
          % (mean_null, sd_null, gap, "通过" if null_ok else "**失败**"))
    print("    注：Δw 均值本身不为 0（%+.2fpp）不是 bug —— 见 [5]。" % mean_null)

    # [5] 排序单调性：桶内按 score 取前 5，是否比 6~10 名更好？（informational）
    exc_ = R["exc_%d" % H]
    t5, r6 = [], []
    for j in range(pool.shape[1]):
        cand = np.where(pool[:, j] & np.isfinite(exc_[:, j]))[0]
        if cand.size < 10:
            continue
        o = cand[np.argsort(-score[cand, j], kind="stable")]
        e = exc_[o, j]
        t5.append(e[:5].mean())
        r6.append(e[5:10].mean())
    if t5:
        print("[5] 桶内排序单调性（informational）：前 5 名超额 %+.3fpp vs 6~10 名 %+.3fpp → %s"
              % (float(np.mean(t5)), float(np.mean(r6)),
                 "打分在头部是反预测的（换席位本身就占便宜）" if float(np.mean(t5)) < float(np.mean(r6))
                 else "打分方向正确"))

    print("\n结论：%s" % ("全部通过" if ok else "**有检查未通过，上面所有表不要读**"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Step 5：前 N 只口径的三臂")
    ap.add_argument("--stage", default="all", choices=("pool", "run", "check", "all"))
    a = ap.parse_args(argv)
    if a.stage in ("pool", "all"):
        meta = stage_pool()
        print(json.dumps({k: v for k, v in meta.items() if k != "npz"},
                         ensure_ascii=False, indent=1), flush=True)
    if a.stage in ("run", "all"):
        stage_run()
    if a.stage in ("check", "all"):
        return stage_check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
