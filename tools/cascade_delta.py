#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""级联增量验证：把「剔除器」和「随机剔同样多」分离开。

要回答的问题
------------
11 条 oss 规则在 3 年窗口上全部反向（详见 backtest_oss_report_20261105.md）。
反向只说明「命中就该回避」，**不说明把它从池子里剔掉就能提高胜率** ——
因为剔掉 45% 的票本身就换了一个基准。所以这个实验的因变量不是「剔完池子好不好」，
而是「**剔得对不对**」：相对等量随机剔除，有没有净增量。

三臂（每条规则 × 每个持有期各算一遍）
--------------------------------------
  U 臂 = 全市场可交易池                     → 不许动的基准
  R 臂 = U 剔除该规则命中的票               → 规则剔除法
  C 臂 = U 剔掉**随机抽的同样多个**票（50 次独立抽样取均值） → 纯缩小池子效应
两个日序列：m(d)=池内日等权超额（已扣 0.25% 成本），w(d)=池内超额>0 的占比（池内胜率）。

两个差序列（必须都报）
----------------------
  Δ_rule   = m_R − m_C   ← 主指标：相对随机剔除的净增量
  Δ_shrink = m_C − m_U   ← 对照：纯粹因为池子变小带来的效应

判定（先定死，跑完不改）
------------------------
  真增量      : Δ_rule>0 且 nw_t(Δ_rule, lag=H)>2 且 6 个半年段里 ≥4 段 Δ_rule>0
  换基准嫌疑  : |S_r|/|U| > 20% → 直接标注「剔除器不可用」
  纯缩小效应  : Δ_rule 不显著、而 Δ_shrink 显著为正 → 正向来自池子变小，不是规则有信息
  无增量      : 其余

为什么 C 臂的「50 次随机抽样」在网格里用补集均值代替
----------------------------------------------------
随机抽 k 个的样本均值，其**期望**等于补集均值，50 次平均只是逼近它。
主表 7 行我实测了 50 次抽样，并逐日报了 max|m_C(50次) − m_C(补集均值)|，
所以网格里的替代是可以被验证的，不是偷工。

数据源与口径
------------
与 tools/backtest_oss.py 完全同源同口径 —— 直接 import 它的 stock_features / rule_hits /
nw_tstat，避免两份实现漂移。信号日 d 收盘出信号 → d+1 收盘建仓 → d+1+H 收盘平仓；
超额 = 个股价收益 − 同日全市场等权收益 − 0.25%。

阶段
----
  --stage matrix   构建公共底座（726 信号日 × 全部票 × 13 个持有期），落 npz
  --stage analyze  从 npz 跑 Step 1~4，出主表 / 旁证矩阵 / 判定
  --stage qfq      Step 6：换前复权库重建同一张主表，与后复权版并排
  --stage all      依次全跑

用法：
    python tools/cascade_delta.py --stage matrix
    python tools/cascade_delta.py --stage analyze
    python tools/cascade_delta.py --stage qfq

★ 只读 kbase.db / app.db / kline_daily.db，只写 viewer/logs/ 与 docs/，不碰 viewer/*.py。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest_oss as B                                     # noqa: E402  同口径的唯一来源

try:
    import numpy as np
except ImportError:                                           # pragma: no cover
    sys.exit("需要 numpy：请用 .venv/Scripts/python.exe 运行")

KBASE = ROOT / "viewer" / "kbase.db"
QFQ = ROOT / "data" / "kline_daily.db"
APP_DB = ROOT / "viewer" / "app.db"
LOG_DIR = ROOT / "viewer" / "logs"
DOC_DIR = ROOT / "docs"

SINCE = "2023-11-04"
UNTIL = "2026-11-04"
STAMP = "20261105"

# 主表：7 条「3 年窗口上稳定反向」的规则 × 各自分类规定的持有期
MAIN_ROWS: tuple[tuple[str, int], ...] = (
    ("oss_burst_vol", 3),
    ("oss_burst", 3),
    ("oss_shrink_vol", 10),
    ("oss_rsi_overbought", 10),
    ("oss_pullback_ma20", 20),
    ("oss_mom20", 20),
    ("oss_ma_align", 40),
)

# 旁证网格：11 条 × 6 档（不参与挑选，负结果照原样进表）
GRID_HORIZONS = (3, 10, 20, 40, 60, 90)

# ---- 判定阈值（先定死） ----
SHARE_LIMIT_PCT = 20.0        # |S|/|U| 超过这个比例 → 换基准嫌疑
SIG_T = 2.0                   # Δ_rule 的 Newey-West t 门槛
MIN_SAME_SEG = 4              # 6 个半年段里至少几段 Δ_rule>0
SHRINK_T = 2.0                # Δ_shrink 显著门槛
MC_DRAWS = 50                 # 随机对照抽样次数

CATEGORY_CN = B.CATEGORY_CN
RULE_CN = {
    "oss_burst_vol": "放量暴涨",
    "oss_burst": "日内暴涨",
    "oss_shrink_vol": "缩量",
    "oss_rsi_overbought": "RSI 超买",
    "oss_pullback_ma20": "回踩 MA20",
    "oss_mom20": "20 日动量 >20%",
    "oss_reversal20": "20 日跌幅 >20%",
    "oss_ma_align": "均线多头排列",
    "oss_near_high": "距 52 周高 25% 内",
    "oss_near_high_tight": "距 52 周高 5% 内",
    "oss_high_vol": "高波动闸门",
}


# ================================================================
# 数据源描述（kbase 与 kline_daily 的唯一差异都收敛在这里）
# ================================================================

class DBSpec:
    """db 路径 + 日期列 + 日期格式。除了这三样，两条链路走同一个函数。"""

    def __init__(self, path: Path, date_col: str, int_date: bool, label: str):
        self.path = path
        self.date_col = date_col
        self.int_date = int_date
        self.label = label

    def norm(self, d) -> str:
        if not self.int_date:
            return str(d)
        s = str(int(d))
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])

    def to_db(self, d: str):
        return int(d.replace("-", "")) if self.int_date else d

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect("file:%s?mode=ro" % self.path.as_posix(), uri=True)

    def calendar(self, d0: str, d1: str) -> list[str]:
        con = self.connect()
        rows = con.execute(
            "SELECT DISTINCT %s FROM kline WHERE %s>=? AND %s<=? ORDER BY %s"
            % (self.date_col, self.date_col, self.date_col, self.date_col),
            (self.to_db(d0), self.to_db(d1))).fetchall()
        con.close()
        return [self.norm(r[0]) for r in rows]

    def iter_bars(self, d0: str, d1: str):
        con = self.connect()
        cur = con.execute(
            "SELECT code,%s,c,h,l,v FROM kline"
            " WHERE %s>=? AND %s<=? AND c>0 ORDER BY code,%s"
            % (self.date_col, self.date_col, self.date_col, self.date_col),
            (self.to_db(d0), self.to_db(d1)))
        cur_code = None
        c_buf: list[float] = []
        h_buf: list[float] = []
        l_buf: list[float] = []
        v_buf: list[float] = []
        d_buf: list[str] = []
        for code, date, c, h, l, v in cur:
            if code != cur_code:
                if cur_code is not None:
                    yield cur_code, c_buf, h_buf, l_buf, v_buf, d_buf
                cur_code = str(code)
                c_buf, h_buf, l_buf, v_buf, d_buf = [], [], [], [], []
            c_buf.append(float(c))
            h_buf.append(float(h) if h is not None else float(c))
            l_buf.append(float(l) if l is not None else float(c))
            v_buf.append(float(v) if v is not None else 0.0)
            d_buf.append(self.norm(date))
        if cur_code is not None:
            yield cur_code, c_buf, h_buf, l_buf, v_buf, d_buf
        con.close()


KBASE_SPEC = DBSpec(KBASE, "date", False, "kbase_hfq")
QFQ_SPEC = DBSpec(QFQ, "d", True, "kline_daily_qfq")


# ================================================================
# Step 0：公共底座
# ================================================================

def build_matrix(spec: DBSpec, out_npz: Path, verbose: bool = True) -> dict:
    """构建 (code × 信号日) 的超额矩阵，一次落盘，后面所有实验都读它。

    落盘内容：
      codes[n_stock]            代码表
      dates[n_sig]              信号日（YYYY-MM-DD）
      uni[n_stock, n_sig]       U(d) 可交易池（非ST ∩ 上市≥60 ∩ 当日有行情）
      hit_<rule>[n_stock,n_sig] 命中
      base_<H>[n_stock,n_sig]   该持有期下可计算的池（U ∩ d+1 有行情 ∩ 收益可算）
      exc_<H>[n_stock,n_sig]    超额（百分点，已扣 0.25%），池外为 NaN
    """
    if not spec.path.exists():
        sys.exit("找不到行情库：%s" % spec.path)
    t_start = time.time()

    cal_all = spec.calendar("1990-01-01", UNTIL)
    if not cal_all:
        sys.exit("%s 在 %s 之前没有行情" % (spec.label, UNTIL))
    until = cal_all[-1]
    sig_start_idx = next((i for i, d in enumerate(cal_all) if d >= SINCE), None)
    if sig_start_idx is None:
        sys.exit("窗口起点 %s 晚于库内最后交易日 %s" % (SINCE, until))
    warm_idx = max(0, sig_start_idx - B.WARMUP_BARS)
    d0 = cal_all[warm_idx]
    cal = cal_all[warm_idx:]
    n_cal = len(cal)
    sig_start = sig_start_idx - warm_idx
    n_sig = n_cal - sig_start
    cal_map = {d: i for i, d in enumerate(cal)}

    if verbose:
        print("[0/4] %s 日历：预热起 %s → 信号起 %s → 终点 %s"
              "（%d 根，信号日 %d）" % (spec.label, d0, cal[sig_start], until, n_cal, n_sig),
              flush=True)

    st_codes = B.load_st_codes(APP_DB)
    rules = list(B.ALL_RULES)
    horizons = sorted(set(B.HORIZON_GRID))

    # ---------- 逐票算判据，同时攒命中矩阵 ----------
    t0 = time.time()
    hit_cols: dict[str, list[np.ndarray]] = {r: [] for r in rules}
    code_list: list[str] = []
    close_cols: list[np.ndarray] = []
    gidx_cols: list[np.ndarray] = []

    for code, cb, hb, lb, vb, db in spec.iter_bars(d0, until):
        c = np.asarray(cb, dtype=np.float64)
        h = np.asarray(hb, dtype=np.float64)
        l = np.asarray(lb, dtype=np.float64)
        v = np.asarray(vb, dtype=np.float64)
        if c.size < B.MIN_LISTED_BARS:
            continue
        gidx = np.array([cal_map.get(d, -1) for d in db], dtype=np.int64)
        keep = gidx >= 0
        if keep.sum() < B.MIN_LISTED_BARS:
            continue
        gidx = gidx[keep]
        feat = B.stock_features(c[keep], h[keep], l[keep], v[keep])
        code_list.append(code)
        close_cols.append(c[keep])
        gidx_cols.append(gidx)
        sig_mask = gidx >= sig_start
        sig_pos = gidx[sig_mask] - sig_start
        for r in rules:
            col = np.zeros(n_sig, dtype=bool)
            col[sig_pos] = B.rule_hits(feat, r)[sig_mask]
            hit_cols[r].append(col)

    n_stock = len(code_list)
    if n_stock == 0:
        sys.exit("没有可用的票（%s，窗口 %s~%s）" % (spec.label, d0, until))
    hits = {r: np.vstack(hit_cols[r]) for r in rules}
    if verbose:
        print("[1/4] 判据：%d 只票 × %d 个信号日（%.1fs）"
              % (n_stock, n_sig, time.time() - t0), flush=True)

    # ---------- 对齐矩阵 ----------
    t0 = time.time()
    mat = np.full((n_stock, n_cal), np.nan, dtype=np.float32)
    for s in range(n_stock):
        mat[s, gidx_cols[s]] = close_cols[s].astype(np.float32)
    present = np.isfinite(mat)
    bars_upto = np.cumsum(present, axis=1, dtype=np.int32)
    st_vec = np.array([c in st_codes for c in code_list], dtype=bool)
    idx = np.where(present, np.arange(n_cal)[None, :], 0)
    np.maximum.accumulate(idx, axis=1, out=idx)
    filled = np.take_along_axis(mat, idx, axis=1)       # 平仓端停牌顺延
    if verbose:
        print("[2/4] 对齐矩阵 %d × %d（%.1fs）"
              % (n_stock, n_cal, time.time() - t0), flush=True)

    # ---------- 每个持有期：池 + 超额 ----------
    t0 = time.time()
    ts_of = {H: np.arange(sig_start, n_cal - H - 1) for H in horizons}
    uni_cols = np.arange(sig_start, n_cal)              # U(d) 定义在信号日上
    uni = present[:, uni_cols] & (~st_vec)[:, None] & (bars_upto[:, uni_cols] >= B.MIN_LISTED_BARS)

    store: dict[str, np.ndarray] = {}
    store["codes"] = np.array(code_list)
    store["dates"] = np.array([cal[i] for i in uni_cols])
    store["uni"] = uni
    for r in rules:
        store["hit_" + r] = hits[r]

    nonempty = 0
    cells = 0
    for H in horizons:
        ts = ts_of[H]
        base_full = np.zeros((n_stock, n_sig), dtype=bool)
        exc_full = np.full((n_stock, n_sig), np.nan, dtype=np.float32)
        entry = mat[:, ts + 1]
        exitp = filled[:, ts + 1 + H]
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = (exitp / entry - 1.0) * 100.0
        ret = np.where(np.isfinite(ret), ret, np.nan)
        base = present[:, ts] & (~st_vec)[:, None] & (bars_upto[:, ts] >= B.MIN_LISTED_BARS) \
            & present[:, ts + 1] & np.isfinite(ret)
        cnt_b = base.sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            bench = np.where(cnt_b > 0,
                             np.where(base, ret, 0.0).sum(axis=0) / np.maximum(cnt_b, 1),
                             np.nan)
        excess = ret - bench[None, :] - B.COST_PCT
        excess = np.where(base, excess, np.nan).astype(np.float32)
        w = ts.size
        base_full[:, :w] = base
        exc_full[:, :w] = excess
        store["base_%d" % H] = base_full
        store["exc_%d" % H] = exc_full
        cells += n_stock * w
        nonempty += int(np.isfinite(excess).sum())
    if verbose:
        print("[3/4] 持有期矩阵：%d 档（%.1fs）" % (len(horizons), time.time() - t0), flush=True)

    out_npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez(out_npz, **store)
    meta = {
        "stage": "matrix",
        "db": spec.label,
        "path": str(spec.path),
        "window": {"since": SINCE, "until": UNTIL,
                   "signal_first": cal[sig_start], "signal_last": cal[-1]},
        "n_signal_days": int(n_sig),
        "n_stocks": int(n_stock),
        "horizons": horizons,
        "cells_total": int(cells),
        "cells_nonempty": int(nonempty),
        "fill_rate_pct": round(100.0 * nonempty / cells, 2) if cells else None,
        "st_excluded_codes": len(st_codes),
        "elapsed_s": round(time.time() - t_start, 1),
        "npz": str(out_npz),
        "npz_mb": round(out_npz.stat().st_size / 1048576.0, 1),
    }
    if verbose:
        print("[4/4] 落盘 %s（%.1f MB）\n      信号日 %d / 票 %d / 非空单元 %d / 填充率 %.2f%%"
              % (out_npz, meta["npz_mb"], n_sig, n_stock, nonempty, meta["fill_rate_pct"]),
              flush=True)
    return meta


# ================================================================
# Step 1~4：三臂分析
# ================================================================

def _arm(exc: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """池内日等权超额 / 池内胜率 / 池内只数。mask 必须是 base 的子集。"""
    n = mask.sum(axis=0).astype(np.int64)
    fill = np.where(mask, exc, 0.0)
    s = fill.sum(axis=0)
    up = (fill > 0).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        m = np.where(n > 0, s / np.maximum(n, 1), np.nan)
        w = np.where(n > 0, up / np.maximum(n, 1), np.nan)
    return m, w, n


def _seg_pos(dates: np.ndarray, x: np.ndarray) -> tuple[int, list[dict]]:
    """6 个半年段里 x>0 的段数，外加逐段明细。"""
    detail = []
    pos = 0
    for s0, s1, label in B.SEGMENTS:
        sel = (dates >= s0) & (dates < s1) & np.isfinite(x)
        if sel.sum() == 0:
            detail.append({"label": label, "n_days": 0, "mean": None, "dir": "无样本"})
            continue
        me = float(np.mean(x[sel]))
        if me > 0:
            pos += 1
        detail.append({"label": label, "n_days": int(sel.sum()),
                       "mean": round(me, 4),
                       "dir": "+" if me > 0 else ("-" if me < 0 else "0")})
    return pos, detail


def _mc_arm(exc: np.ndarray, base: np.ndarray, hit: np.ndarray, k: np.ndarray,
            draws: int, seed: int) -> tuple[np.ndarray, np.ndarray, float]:
    """C 臂：每天从 **U(d) 全体**里随机抽 |S_r(d)| 个剔掉，抽 draws 次，取「剩下那池」的均值。

    ⚠️ 这里有个极容易写错的地方（我第一版就写错了，记下来）：
      随机抽样的**总体**必须是 U，不能是 U\\S。
      如果从 U\\S 里抽，抽到的样本均值期望就等于补集均值，于是
      Δ_rule = m_R − m_C 恒等于 0 ± 蒙特卡洛噪声 —— 实验白做，而且结果长得
      「很干净」（全是 ±0.00x），最容易骗过自己。
      正确的 C 臂才是真正的反事实：「如果我非要从池子里砍掉 k 只，砍随机 k 只
      和砍规则选出的那 k 只，剩下那池谁更好」。

    数学上 E[m_C] = m_U（每只票被剔的概率相同，剩下的池均值的期望就是原池均值），
    所以 Δ_shrink = m_C − m_U 的期望恒为 0 —— 这不是缺陷，它是本实验的**空白对照**：
    它的实测噪声给出「Δ_rule 要多大量级才算有信息」的标尺。
    返回 (日均值, 日胜率, 空白对照偏离 |m_C − m_U| 的最大值)。
    """
    rng = np.random.default_rng(seed)
    n_sig = exc.shape[1]
    m = np.full(n_sig, np.nan)
    w = np.full(n_sig, np.nan)
    m_U, _w_U, _n_U = _arm(exc, base)
    worst = 0.0
    for d in range(n_sig):
        kd = int(k[d])
        cols = np.where(base[:, d])[0]
        n = cols.size
        if kd <= 0 or n <= kd:
            m[d] = m_U[d]
            w[d] = np.nan
            continue
        vals = exc[cols, d].astype(np.float64)
        up = (vals > 0)
        rnd = rng.random((draws, n), dtype=np.float64)
        pick = np.argpartition(rnd, kd - 1, axis=1)[:, :kd]
        kept_n = n - kd
        m_kept = (vals.sum() - vals[pick].sum(axis=1)) / kept_n
        w_kept = (up.sum() - up[pick].sum(axis=1)) / kept_n
        m[d] = float(m_kept.mean())
        w[d] = float(w_kept.mean())
        if np.isfinite(m_U[d]):
            worst = max(worst, abs(m[d] - m_U[d]))
    return m, w, worst


def _verdict(share_pct: float, d_rule: float, nw_rule: float, seg_pos: int,
             nw_shrink: float) -> tuple[str, str]:
    """判定（阈值先定死，跑完不许改）。"""
    if share_pct is not None and share_pct > SHARE_LIMIT_PCT:
        return "换基准嫌疑", "share_gt_%g" % SHARE_LIMIT_PCT
    if (d_rule is not None and d_rule > 0
            and nw_rule is not None and np.isfinite(nw_rule) and nw_rule > SIG_T
            and seg_pos >= MIN_SAME_SEG):
        return "真增量", "pass"
    if (nw_shrink is not None and np.isfinite(nw_shrink) and nw_shrink > SHRINK_T):
        return "纯缩小效应", "shrink_only"
    return "无增量", "no_gain"


def _econ_grade(d_rule: float | None) -> tuple[str, str]:
    """经济量级门槛（主理人 2026-11-05 追加，属于**后加的判据**，与本实验预注册阈值分开列）。

    理由：一次进出的成本是 25bp。剔除器把池子均值抬高多少，必须和 25bp 比才有意义。
      ≥ 0.25pp  → 至少赚回一次成本，才允许写「可用」
      0.05~0.25 → 有信息但不可用（信息量小于交易成本）
      < 0.05    → 无经济意义
    """
    if d_rule is None:
        return "无数据", "no_data"
    if d_rule >= 0.25:
        return "可用", "usable"
    if d_rule >= 0.05:
        return "有信息但不可用", "info_not_usable"
    return "无经济意义", "no_econ"


def _row(exc: np.ndarray, base: np.ndarray, hit: np.ndarray, H: int,
         dates: np.ndarray, mc=None) -> dict:
    m_U, w_U, n_U = _arm(exc, base)
    m_S, w_S, n_S = _arm(exc, hit & base)
    rest = base & ~hit
    m_R, w_R, n_R = _arm(exc, rest)
    if mc is None:
        # 网格里不跑 50 次抽样：随机剔 k 只后的剩池均值，其期望就是原池均值 m_U
        # （已用主表 7 行实测 50 次抽样验证，最大偏离见 mc_gap 列），所以用 m_U 代替。
        m_C, w_C = m_U.copy(), w_U.copy()
        mc_gap = None
    else:
        m_C, w_C, mc_gap = mc
    d_rule = m_R - m_C
    d_shrink = m_C - m_U
    dw_rule = w_R - w_C
    dw_shrink = w_C - w_U
    seg_pos, seg_detail = _seg_pos(dates, d_rule)
    share = 100.0 * np.nansum(n_S) / max(np.nansum(n_U), 1)
    d_rule_mean = float(np.nanmean(d_rule)) if np.isfinite(d_rule).any() else None
    d_shrink_mean = float(np.nanmean(d_shrink)) if np.isfinite(d_shrink).any() else None
    nw_rule = B.nw_tstat(d_rule, H)
    nw_shrink = B.nw_tstat(d_shrink, H)
    v, code = _verdict(share, d_rule_mean, nw_rule, seg_pos, nw_shrink)
    return {
        "H": H,
        "n_days": int(np.isfinite(d_rule).sum()),
        "share_pct": round(float(share), 2),
        "n_hit_samples": int(np.nansum(n_S)),
        "m_U": round(float(np.nanmean(m_U)), 4),
        "m_S": round(float(np.nanmean(m_S)), 4),
        "m_R": round(float(np.nanmean(m_R)), 4),
        "m_C": round(float(np.nanmean(m_C)), 4),
        "d_shrink": round(float(d_shrink_mean), 4),
        "d_rule": round(float(d_rule_mean), 4),
        "t_rule": round(B.tstat(d_rule), 2),
        "nw_rule": round(float(nw_rule), 2) if np.isfinite(nw_rule) else None,
        "t_shrink": round(B.tstat(d_shrink), 2),
        "nw_shrink": round(float(nw_shrink), 2) if np.isfinite(nw_shrink) else None,
        "w_U": round(float(np.nanmean(w_U)) * 100.0, 2),
        "w_S": round(float(np.nanmean(w_S)) * 100.0, 2),
        "w_R": round(float(np.nanmean(w_R)) * 100.0, 2),
        "w_C": round(float(np.nanmean(w_C)) * 100.0, 2) if np.isfinite(w_C).any() else None,
        "dw_rule": round(float(np.nanmean(dw_rule)) * 100.0, 2) if np.isfinite(dw_rule).any() else None,
        "dw_shrink": round(float(np.nanmean(dw_shrink)) * 100.0, 2) if np.isfinite(dw_shrink).any() else None,
        "seg_pos": seg_pos,
        "seg_detail": seg_detail,
        "mc_gap": round(mc_gap, 4) if mc_gap is not None else None,
        "verdict": v,
        "verdict_code": code,
        "econ_grade": _econ_grade(d_rule_mean)[0],
        "econ_code": _econ_grade(d_rule_mean)[1],
    }


def analyze(npz_path: Path, out_json: Path, draws: int = MC_DRAWS,
            verbose: bool = True) -> dict:
    t0 = time.time()
    z = np.load(npz_path, allow_pickle=False)
    dates = z["dates"]
    meta = {
        "npz": str(npz_path),
        "n_signal_days": int(dates.size),
        "n_stocks": int(z["codes"].size),
        "first_day": str(dates[0]),
        "last_day": str(dates[-1]),
    }
    if verbose:
        print("[1/4] 读底座：%d 信号日 × %d 票（%s ~ %s）"
              % (meta["n_signal_days"], meta["n_stocks"], meta["first_day"],
                 meta["last_day"]), flush=True)

    # ---------- 主表：7 条 × 各自分类持有期 ----------
    main_rows = []
    for i, (rule, H) in enumerate(MAIN_ROWS):
        exc = z["exc_%d" % H].astype(np.float64)
        base = z["base_%d" % H]
        hit = z["hit_" + rule]
        k = (hit & base).sum(axis=0)
        mc = _mc_arm(exc, base, hit, k, draws, seed=20261105 + i)
        row = _row(exc, base, hit, H, dates, mc=mc)
        row.update({"rule": rule, "rule_cn": RULE_CN.get(rule, rule),
                    "category": B.RULE_CATEGORY[rule],
                    "category_cn": CATEGORY_CN[B.RULE_CATEGORY[rule]]})
        main_rows.append(row)
        if verbose:
            print("  %-22s T+%-3d 命中占比 %5.1f%%  Δ_rule %+7.3f  nw %6.2f  段 %d/6  → %s"
                  % (rule, H, row["share_pct"], row["d_rule"], row["nw_rule"] or 0.0,
                     row["seg_pos"], row["verdict"]), flush=True)

    # ---------- 旁证网格：11 条 × 6 档 ----------
    grid_rows = []
    for rule in B.ALL_RULES:
        for H in GRID_HORIZONS:
            exc = z["exc_%d" % H].astype(np.float64)
            base = z["base_%d" % H]
            hit = z["hit_" + rule]
            row = _row(exc, base, hit, H, dates)
            row.update({"rule": rule, "rule_cn": RULE_CN.get(rule, rule)})
            grid_rows.append(row)
    if verbose:
        print("[2/4] 旁证网格：%d 格（%d 条 × %d 档，%.1fs）"
              % (len(grid_rows), len(B.ALL_RULES), len(GRID_HORIZONS),
                 time.time() - t0), flush=True)

    # ---------- 汇总 ----------
    by_verdict: dict[str, list[str]] = {}
    for r in main_rows:
        by_verdict.setdefault(r["verdict"], []).append("%s(T+%d)" % (r["rule"], r["H"]))
    grid_pass = [g for g in grid_rows if g["verdict_code"] == "pass"]
    payload = {
        "meta": dict(meta, elapsed_s=round(time.time() - t0, 1)),
        "thresholds": {
            "share_limit_pct": SHARE_LIMIT_PCT,
            "sig_t_nw": SIG_T,
            "min_same_seg": MIN_SAME_SEG,
            "shrink_t_nw": SHRINK_T,
            "mc_draws": draws,
            "cost_pct": B.COST_PCT,
            "min_listed_bars": B.MIN_LISTED_BARS,
        },
        "main": main_rows,
        "grid": grid_rows,
        "summary": {
            "main_pass": [r["rule"] for r in main_rows if r["verdict_code"] == "pass"],
            "by_verdict": by_verdict,
            "grid_pass_count": len(grid_pass),
            "grid_pass": ["%s@T+%d" % (g["rule"], g["H"]) for g in grid_pass],
            "grid_cells": len(grid_rows),
        },
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if verbose:
        print("[3/4] 落盘 %s" % out_json, flush=True)
        print("[4/4] 主表判定：" + "；".join(
            "%s=%d" % (k, len(v)) for k, v in by_verdict.items()), flush=True)
    return payload


# ================================================================
# Step 6：换前复权基准复核
# ================================================================

def coverage_gate(days_required: int = 700) -> dict:
    """覆盖率闸门：不够就不许跑，直接写「不可复核」。

    主理人给的硬条件：`data/kline_daily.db` 在 2023-11-06~2026-11-04 的覆盖率 <90%
    就不跑。这里把「覆盖」定义清楚：kbase 窗口内出现过的票，有多少在 qfq 里也有行情。
    """
    k = KBASE_SPEC.calendar("1990-01-01", UNTIL)
    q = QFQ_SPEC.calendar("1990-01-01", UNTIL)
    k_codes = set()
    con = KBASE_SPEC.connect()
    for (c,) in con.execute("SELECT DISTINCT code FROM kline WHERE date>=? AND date<=?",
                            (SINCE, UNTIL)):
        k_codes.add(str(c))
    con.close()
    q_codes = set()
    con = QFQ_SPEC.connect()
    for (c,) in con.execute("SELECT DISTINCT code FROM kline WHERE d>=? AND d<=?",
                            (QFQ_SPEC.to_db("2023-11-06"), QFQ_SPEC.to_db(UNTIL))):
        q_codes.add(str(c))
    con.close()
    k_days = [d for d in k if SINCE <= d <= UNTIL]
    q_days = [d for d in q if SINCE <= d <= UNTIL]
    inter = k_codes & q_codes
    return {
        "kbase_codes": len(k_codes), "qfq_codes": len(q_codes),
        "common_codes": len(inter),
        "code_coverage_pct": round(100.0 * len(inter) / max(len(k_codes), 1), 2),
        "kbase_days": len(k_days), "qfq_days": len(q_days),
        "only_in_qfq": len(q_codes - k_codes), "only_in_kbase": len(k_codes - q_codes),
        "gate_pass": (len(inter) / max(len(k_codes), 1) >= 0.90) and len(q_days) >= days_required
                     and len(k_days) >= days_required,
    }


def compare_hfq_qfq(hfq_json: Path, qfq_json: Path, out_md: Path) -> dict:
    """两版主表并排：方向一致就加固，翻转就以翻转为准（不许取平均）。"""
    A = json.loads(hfq_json.read_text(encoding="utf-8"))
    Bp = json.loads(qfq_json.read_text(encoding="utf-8"))
    amap = {r["rule"]: r for r in A["main"]}
    bmap = {r["rule"]: r for r in Bp["main"]}
    rows = []
    flip = []
    for rule, _ in MAIN_ROWS:
        a, b = amap.get(rule), bmap.get(rule)
        if not a or not b:
            continue
        same_sign = (a["d_rule"] > 0) == (b["d_rule"] > 0)
        same_verdict = a["verdict_code"] == b["verdict_code"]
        rows.append({
            "rule": rule, "rule_cn": RULE_CN.get(rule, rule), "H": a["H"],
            "hfq_d_rule": a["d_rule"], "hfq_nw": a["nw_rule"], "hfq_verdict": a["verdict"],
            "qfq_d_rule": b["d_rule"], "qfq_nw": b["nw_rule"], "qfq_verdict": b["verdict"],
            "same_sign": bool(same_sign), "same_verdict": bool(same_verdict),
            "hfq_m_U": a["m_U"], "qfq_m_U": b["m_U"],
            "hfq_share": a["share_pct"], "qfq_share": b["share_pct"],
        })
        if not same_verdict:
            flip.append(rule)
    payload = {
        "hfq_meta": A["meta"], "qfq_meta": Bp["meta"],
        "rows": rows,
        "n_same_sign": sum(1 for r in rows if r["same_sign"]),
        "n_same_verdict": sum(1 for r in rows if r["same_verdict"]),
        "flipped": flip,
        "conclusion": ("两版判定一致，主结论加固" if not flip
                       else "有 %d 条判定翻转，以翻转版为准：%s" % (len(flip), "、".join(flip))),
    }
    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 后复权 vs 前复权 并排", "",
             "| 规则 | 持有期 | 后复权 Δ_rule | nw | 判定 | 前复权 Δ_rule | nw | 判定 | 同向 | 同判定 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | T+%d | %+.3f | %s | %s | %+.3f | %s | %s | %s | %s |" % (
            r["rule_cn"], r["H"], r["hfq_d_rule"], r["hfq_nw"], r["hfq_verdict"],
            r["qfq_d_rule"], r["qfq_nw"], r["qfq_verdict"],
            "是" if r["same_sign"] else "**否**", "是" if r["same_verdict"] else "**否**"))
    lines += ["", payload["conclusion"]]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


# ================================================================
# CLI
# ================================================================

def main(argv=None) -> int:
    global SINCE, UNTIL
    ap = argparse.ArgumentParser(description="级联增量验证（U/R/C 三臂）")
    ap.add_argument("--stage", default="all",
                    choices=("matrix", "analyze", "qfq", "gapattr", "poolsize",
                             "compare", "all"))
    ap.add_argument("--since", default=SINCE)
    ap.add_argument("--until", default=UNTIL)
    ap.add_argument("--stamp", default=STAMP)
    ap.add_argument("--draws", type=int, default=MC_DRAWS)
    a = ap.parse_args(argv)

    SINCE, UNTIL = a.since, a.until
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    npz_hfq = LOG_DIR / ("cascade_matrix_%s.npz" % a.stamp)
    npz_qfq = LOG_DIR / ("cascade_matrix_qfq_%s.npz" % a.stamp)
    json_hfq = LOG_DIR / ("cascade_delta_%s.json" % a.stamp)
    json_qfq = LOG_DIR / ("cascade_delta_qfq_%s.json" % a.stamp)

    if a.stage in ("matrix", "all"):
        meta = build_matrix(KBASE_SPEC, npz_hfq)
        (LOG_DIR / ("cascade_matrix_%s.meta.json" % a.stamp)).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n== Step 0 完成 ==\n" + json.dumps(meta, ensure_ascii=False, indent=2))
    if a.stage in ("analyze", "all"):
        analyze(npz_hfq, json_hfq, draws=a.draws)
    if a.stage == "gapattr" or a.stage == "all":
        if not npz_qfq.exists():
            print("缺 qfq 底座 %s，先跑 --stage qfq" % npz_qfq)
        else:
            print("\n== Step 6b 污染归因分解 ==")
            gap_attribution(npz_hfq, npz_qfq,
                            LOG_DIR / ("cascade_gap_attr_%s.json" % a.stamp))
    if a.stage == "poolsize":
        src = npz_qfq if npz_qfq.exists() else npz_hfq
        print("\n== 池子规模扫描（主版本：%s）==" % src.name)
        pool_scan(src, LOG_DIR / ("cascade_pool_scan_%s.json" % a.stamp))
    if a.stage == "qfq" or a.stage == "all":
        cov = coverage_gate()
        print("\n== Step 6 覆盖闸门 ==")
        print(json.dumps(cov, ensure_ascii=False, indent=2))
        (LOG_DIR / ("cascade_coverage_%s.json" % a.stamp)).write_text(
            json.dumps(cov, ensure_ascii=False, indent=2), encoding="utf-8")
        if not cov["gate_pass"]:
            print("覆盖不足 → 不跑前复权版，结论写「不可复核，原因是前复权库覆盖不足」")
        else:
            build_matrix(QFQ_SPEC, npz_qfq)
            analyze(npz_qfq, json_qfq, draws=a.draws)
    return 0




# ================================================================
# Step 6b：污染归因分解（命中集拆成「污染命中」和「干净命中」）
# ================================================================

# 每条规则真实回看窗口（bar 数）——不是拍脑袋，按 feature 定义取：
#   ret1 → 1 根；volume_ratio → 5 根；rsi6(Wilder) → 6；atr14 → 14；
#   ma20 → 20；mom20/ret20 → 21 根（c/c[-21]）；ma60 → 60；dist_high250 → 251 根
RULE_LOOKBACK: dict[str, int] = {
    "oss_burst": 1,            # ret1
    "oss_burst_vol": 5,        # ret1 + volume_ratio(5 日均量)
    "oss_shrink_vol": 5,       # volume_ratio
    "oss_rsi_overbought": 6,   # rsi6
    "oss_high_vol": 14,        # atr14
    "oss_pullback_ma20": 20,   # ma20
    "oss_mom20": 21,           # c/c[-21]
    "oss_reversal20": 21,      # c/c[-21]
    "oss_ma_align": 60,        # ma60
    "oss_near_high": 251,      # 251 根最高收盘
    "oss_near_high_tight": 251,
}


def gap_mask(dates: np.ndarray, codes: np.ndarray, gap_pct: float = 0.5):
    """(code, date) 上的「除权缺口」掩码：kbase 日收益 与 qfq 日收益 相差超过 gap_pct。

    这是本实验里唯一需要同时读两个库的地方：缺口只能通过**两库相减**定义，
    单看任何一个库都看不出来（kbase 自洽、qfq 也自洽，差别只在除权日）。
    """
    kd: dict[str, tuple] = {}
    con = KBASE_SPEC.connect()
    for code, d, c in con.execute(
            "SELECT code,date,c FROM kline WHERE date>=? AND date<=? AND c>0 ORDER BY code,date",
            (SINCE, UNTIL)):
        kd.setdefault(str(code), ([], []))
        kd[str(code)][0].append(str(d))
        kd[str(code)][1].append(float(c))
    con.close()
    qd: dict[str, tuple] = {}
    con = QFQ_SPEC.connect()
    for code, d, c in con.execute(
            "SELECT code,d,c FROM kline WHERE d>=? AND d<=? AND c>0 ORDER BY code,d",
            (QFQ_SPEC.to_db("2023-11-01"), QFQ_SPEC.to_db(UNTIL))):
        qd.setdefault(str(code), ([], []))
        qd[str(code)][0].append(QFQ_SPEC.norm(d))
        qd[str(code)][1].append(float(c))
    con.close()

    day_idx = {d: i for i, d in enumerate(dates)}
    n, m = codes.size, dates.size
    mask = np.zeros((n, m), dtype=bool)
    for i, code in enumerate(codes):
        if code not in kd or code not in qd:
            continue
        kdd, kc = kd[code]
        qdd, qc = qd[code]
        km = {d: j for j, d in enumerate(kdd)}
        qm = {d: j for j, d in enumerate(qdd)}
        for j in range(1, len(kdd)):
            d, dp = kdd[j], kdd[j - 1]
            if d not in day_idx or dp not in qm or d not in qm:
                continue
            qi, qj = qm[dp], qm[d]
            if qj != qi + 1:            # 只在两边都连续时相减
                continue
            rk = (kc[j] / kc[j - 1] - 1.0) * 100.0
            rq = (qc[qj] / qc[qi] - 1.0) * 100.0
            if abs(rk - rq) > gap_pct:
                mask[i, day_idx[d]] = True
    return mask


def lookback_any(mask: np.ndarray, L: int) -> np.ndarray:
    """截至当日的 L 根 bar 内是否出现过 True（含当日）。"""
    if L <= 1:
        return mask.copy()
    c = np.cumsum(mask, axis=1, dtype=np.int32)
    out = c > 0
    if L < mask.shape[1]:
        out[:, L:] = c[:, L:] - c[:, :-L] > 0
    return out


# 归因表要覆盖全部 11 条（不只那 7 条）：`reversal20` 与两条 `near_high` 才是
# 「缺口伪造触发条件」的最典型受害者，只看 7 条会漏掉它们。
GAPATTR_ROWS: tuple[tuple[str, int], ...] = MAIN_ROWS + (
    ("oss_reversal20", 20),
    ("oss_near_high", 90),
    ("oss_near_high_tight", 90),
    ("oss_high_vol", 20),
)


def gap_attribution(npz_hfq: Path, npz_qfq: Path, out_json: Path,
                    rows: tuple[tuple[str, int], ...] = GAPATTR_ROWS) -> dict:
    """把命中集拆成「污染命中 / 干净命中」，各自算超额与 t（超额一律取 qfq，天然无缺口）。

    判读规则（先写死）：只用干净命中后，原来显著为负的规则若 |t_nw|<2，判定降级为「未验证」。
    """
    Z = np.load(npz_hfq, allow_pickle=False)
    Q = np.load(npz_qfq, allow_pickle=False)
    dates = Z["dates"]
    codes = Z["codes"]
    q_codes = Q["codes"]
    q_pos = {c: i for i, c in enumerate(q_codes)}
    # kbase 票 → qfq 行号（100% 覆盖，理论上不会有缺失）
    map_idx = np.array([q_pos.get(c, -1) for c in codes], dtype=np.int64)
    have_q = map_idx >= 0

    t0 = time.time()
    gm = gap_mask(dates, codes)
    base_rate = 100.0 * gm.mean()
    print("[gap] 缺口掩码：%d 个 (票,日) 命中，占全部单元 %.4f%%（%.1fs）"
          % (int(gm.sum()), base_rate, time.time() - t0), flush=True)

    out = {"window": {"since": SINCE, "until": UNTIL},
           "gap_threshold_pct": 0.5,
           "gap_cells": int(gm.sum()),
           "gap_rate_pct": round(base_rate, 4),
           "rules": []}

    for rule, H in rows:
        L = RULE_LOOKBACK[rule]
        poll = lookback_any(gm, L)
        exc_q = Q["exc_%d" % H].astype(np.float64)
        base_q = Q["base_%d" % H]
        # 把 qfq 的池/超额按 kbase 的票序取出来（没有 qfq 行情的票置无效）
        exc = np.full((codes.size, dates.size), np.nan)
        bas = np.zeros((codes.size, dates.size), dtype=bool)
        exc[have_q] = exc_q[map_idx[have_q]]
        bas[have_q] = base_q[map_idx[have_q]]

        hit = Z["hit_" + rule] & bas
        contam = hit & poll
        clean = hit & ~poll
        # 全池（同一 L）的污染率，作为「规则是否偏爱缺口票」的基准
        uni_poll = 100.0 * (bas & poll).sum() / max(int(bas.sum()), 1)

        def _stat(m):
            sel = m & np.isfinite(exc)
            n_s = int(sel.sum())
            if n_s == 0:
                return {"n": 0, "mean": None, "t": None, "nw": None, "win": None, "days": 0}
            daily = []
            for d in range(dates.size):
                col = exc[:, d][sel[:, d]]
                daily.append(col.mean() if col.size else np.nan)
            daily = np.array(daily)
            return {"n": n_s, "mean": round(float(np.nanmean(exc[sel])), 4),
                    # 两种加权必须都报：`mean` 是按持仓（命中只数）加权，会被「一天涌出一大批」
                    # 主导；`mean_daily` 是按日等权，才是「随机一个信号日的期望」。
                    # 两者符号相反的情况在本数据集里真实存在（见主报告第 8 节第 3 条）。
                    "mean_daily": round(float(np.nanmean(daily)), 4),
                    "t": round(float(B.tstat(daily)), 2) if np.isfinite(daily).sum() > 2 else None,
                    "nw": (round(float(B.nw_tstat(daily, H)), 2)
                           if np.isfinite(B.nw_tstat(daily, H)) else None),
                    "win": round(100.0 * float((exc[sel] > 0).mean()), 2),
                    "days": int(np.isfinite(daily).sum())}

        row = {"rule": rule, "rule_cn": RULE_CN.get(rule, rule), "H": H, "lookback": L,
               "hit_total": int(hit.sum()), "hit_contam": int(contam.sum()),
               "contam_share_pct": round(100.0 * contam.sum() / max(int(hit.sum()), 1), 2),
               "universe_poll_pct_same_L": round(uni_poll, 2),
               "all": _stat(hit), "clean": _stat(clean), "contam_only": _stat(contam)}
        # 判读
        c_nw = row["clean"]["nw"]
        row["downgraded"] = bool(c_nw is not None and abs(c_nw) < 2.0)
        out["rules"].append(row)
        print("  %-22s L=%-3d 命中 %6d 污染 %5d(%5.2f%%, 全池 %.2f%%)  "
              "全部 t_nw %6s → 干净 t_nw %6s %s"
              % (rule, L, row["hit_total"], row["hit_contam"], row["contam_share_pct"],
                 uni_poll, row["all"]["nw"], c_nw, "【降级】" if row["downgraded"] else ""),
              flush=True)

    out["elapsed_s"] = round(time.time() - t0, 1)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ================================================================
# Step 5 替代：池子规模扫描（小池子上剔除器会不会变有用）
# ================================================================

POOL_SIZES = (50, 200, 500, 1000, 0)      # 0 = 全市场
POOL_REPEATS = 20                          # 每个 (日, 规模) 抽 20 次，压掉抽样噪声


def pool_scan(npz_path: Path, out_json: Path, sizes=POOL_SIZES,
              repeats: int = POOL_REPEATS, seed: int = 20261105) -> dict:
    """把 U(d) 随机缩到 N 只，再看 Δ_rule 会不会变大。

    为什么用这个替代「真实产品路径」：主理人要的 Step 5 是「挂在各桶现有工具的日交集之后」，
    但 `stock_indicators_daily` 只有 261 个交易日，而产品快照 `_build_oss_snapshot` 需要
    **251 天历史**才能算 dist_high250 / mom20 —— 能忠实重建的日期只有 **11 天**（2026-10-20 起）。
    11 天不可能支撑任何分段或显著性结论，硬跑只会得到一个看起来像结论的东西。
    所以这里改测**机制**而不是**路径**：如果「小池子上剔除器更有用」这个机制成立，
    那么把 U(d) 随机缩到 50/200/500/1000 只，Δ_rule 就应该单调变大。
    机制不成立，Step 5 就不用等了；机制成立，才值得让工程去落历史命中表。

    ⚠️ 注意这是**随机子集**，不是产品里那种「被其他工具筛过的子集」。
       随机子集只保留了「池子变小」这一个效应，去掉了「池子被选过」的效应。
       所以它给出的是**下界**：如果连随机缩小都没有边际递增，被选过的池子也不会凭空冒出来。
    """
    z = np.load(npz_path, allow_pickle=False)
    dates = z["dates"]
    rng = np.random.default_rng(seed)
    rules = [r for r, _ in MAIN_ROWS]
    hz = {r: h for r, h in MAIN_ROWS}
    # 每个规模一份缓存，避免重复抽同一批随机数
    out = {"meta": {"npz": str(npz_path), "repeats": repeats, "seed": seed,
                    "sizes": list(sizes)}, "rows": []}

    for rule in rules:
        H = hz[rule]
        exc = z["exc_%d" % H].astype(np.float64)
        base = z["base_%d" % H]
        hit = z["hit_" + rule] & base
        n_stock, n_day = exc.shape
        for N in sizes:
            d_rule = np.full(n_day, np.nan)
            share = np.full(n_day, np.nan)
            for d in range(n_day):
                cols = np.where(base[:, d])[0]
                n = cols.size
                if n < 10:
                    continue
                take = n if (N == 0 or N >= n) else N
                if take == n:
                    v = exc[cols, d]
                    h = hit[cols, d]
                    if h.any() and (~h).any():
                        d_rule[d] = float(v[~h].mean() - v.mean())
                        share[d] = float(h.mean())
                    continue
                acc_d = []
                acc_s = []
                for _ in range(repeats):
                    idx = rng.choice(n, take, replace=False)
                    v = exc[cols[idx], d]
                    h = hit[cols[idx], d]
                    if h.any() and (~h).any():
                        acc_d.append(float(v[~h].mean() - v.mean()))
                        acc_s.append(float(h.mean()))
                if acc_d:
                    d_rule[d] = float(np.mean(acc_d))
                    share[d] = float(np.mean(acc_s))
            ok = np.isfinite(d_rule)
            row = {"rule": rule, "rule_cn": RULE_CN.get(rule, rule), "H": H,
                   "pool_size": ("全市场" if N == 0 else N),
                   "n_days": int(ok.sum()),
                   "hit_share_pct": round(100.0 * float(np.nanmean(share)), 2),
                   "d_rule": round(float(np.nanmean(d_rule)), 4),
                   "nw": (round(float(B.nw_tstat(d_rule, H)), 2)
                          if ok.sum() > 10 else None),
                   "econ_grade": _econ_grade(float(np.nanmean(d_rule)))[0]}
            out["rows"].append(row)
            print("  %-22s T+%-3d N=%-6s 命中占比 %5.2f%%  Δ_rule %+7.3f  nw %6s  %s"
                  % (rule, H, ("全市场" if N == 0 else N), row["hit_share_pct"],
                     row["d_rule"], row["nw"], row["econ_grade"]), flush=True)

    # 单调性汇总：seq 按 sizes 顺序（50,200,500,1000,全市场），seq[0] 是最小池子、seq[-1] 是全市场
    print("\n[池子规模单调性]  Δ_rule 随池子缩小是否变大（N=50 vs 全市场）", flush=True)
    for rule in rules:
        rs = [x for x in out["rows"] if x["rule"] == rule]
        seq = [x["d_rule"] for x in rs]
        grow = seq[0] > seq[-1]
        print("  %-22s %s  %s（差 %+.3f）"
              % (rule, " → ".join("%+.3f" % v for v in seq),
                 "随池子缩小变大" if grow else "随池子缩小不变/变小", seq[0] - seq[-1]),
              flush=True)
        out.setdefault("monotonic", []).append(
            {"rule": rule, "seq": seq, "grow": bool(grow),
             "delta_n50_vs_full": round(seq[0] - seq[-1], 4)})

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out



if __name__ == "__main__":
    raise SystemExit(main())
