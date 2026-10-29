# -*- coding: utf-8 -*-
"""量化板块 · 第三维信号源（quant-v1.0，2026-10-16 进哥需求落地）。

定位（架构红线，与 resonance.py / pillars.py 一致）：本模块只在**后端批量任务 / 请求线程只读**里跑，
查看器只读本模块产出的表 + 实时算的技术因子（纯本地 K 线，零网络）。

三支柱（对应设计稿「5 个子视图」）：
  A  因子全景      —— 单票技术面 7 因子打分（雷达/条形），纯离线算
  B  多因子选股    —— 截面用共享引擎（metalearn）给因子加权出清单
  C  因子回测      —— 各因子 IC / IR / 衰减（T+5/T+10/T+20）
  D  菜谱预设      —— 进哥「私家配比」入口；强制报 OOS 外样 Sharpe/胜率
  E  量化因子的自我学习与成长 —— 实例化 metalearn 于因子，带诚实三态徽章

诚实口径（写进 payload.caliber，前端原样展示）：
  - 全部技术因子**纯本地 K 线离线算**（predictor.load_kcache），不依赖同花顺、不硬联网。
  - 基本面（PE/PB/ROE/成长）与情绪/资金因子**需要 akshare/联网，本沙箱不可用**——
    只声明维度、标「待接入」，绝不编造数值、绝不参学。
  - 因子加权**唯一真源 = metalearn.weights_from_ic**（与 E 引擎自加权、evolve 自我进化共用），
    禁止在此再抄第三套 IC/收缩/先验公式。
  - 样本不足（IC 样本 < MIN_IC_N 或全 ≤0）→ 诚实退回等权先验，徽章标「等权运行中」；
    OOS 未通过诚实退回等权，绝不填假权重。
  - 因子按家族（动量/技术形态/量价/质量）先分组降维展示，再在家族内/间用 IC 权重融合，
    **不枚举 2^100 个子集**（让数据自动淘汰弱因子）。
"""
from __future__ import annotations

import bisect
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime

import predictor as P          # viewer/ 同 resonance.py / pillars.py 的依赖方式
from metalearn import (        # 权重学习唯一真源：禁止再抄第三套
    weights_from_ic, pearson, MIN_IC_N, IC_SHRINK_K, W_PRIOR_MIX, W_FLOOR,
)

ENGINE_VER = "quant-v1.0"
MIN_BARS = P.MIN_BARS          # 复用 predictor 的 130 根下限（与共振/每日预测一致）
LOOKBACK = 65                  # 因子计算需要的最长窗口（MA60 + KDJ/BOLL 余量）
BT_DATES = 120                 # 回测/学习最多使用的历史交易日数（控成本）
# 2026-10-29：因子取值所需的最短窗口 = 最长因子窗口 + 回测期 + 前向收益期 = 65+120+20 = 205。
# 取 260 留余量；超过这个长度的历史对结果没有任何影响，却会把内存撑爆（见 _universe_bars）。
_SERIES_TAIL = 260
TOP_N_SELECT = 30              # 多因子选股默认出清单只数
OOS_MIN = 5                    # 样本外验证最少可比期数（walk-forward 比较次数门槛）
OOS_WIN = 0.60                 # 加权方相对等权方的胜率门槛
PANEL_STEP = 8                 # walk-forward 测试期步长（控制 OOS 计算量）

# ---- 可学技术因子（离线、真实可用）------------------------------------------
# 每个因子都有「oriented」取值：方向已对齐成「越大越好」（higher = 更看多/更稳），
# 这样 IC 的正负能直接读出「该因子对未来超额的方向性」。
TECH_FACTORS = ["MA_POS", "MACD", "KDJ", "BOLL", "VOL_ABS", "MOM", "VOLA"]
FACTOR_META = {
    "MA_POS":  {"label": "均线多空",   "family": "动量",     "unit": "%",  "scale": 8.0,
                "desc": "价格相对 MA20 的位置 + 均线排列（MA5>MA10>MA20>MA60 给加分）；越大越偏多头"},
    "MACD":    {"label": "MACD 动能",  "family": "技术形态", "unit": "%",  "scale": 300.0,
                "desc": "DIF-DEA 柱（histogram）占收盘价比例；红柱放大为正、死叉为负"},
    "KDJ":     {"label": "KDJ 强度",   "family": "技术形态", "unit": "",   "scale": 1.0,
                "desc": "J 值相对中轴 50 的偏离（作动量解读：J 高=上行动能强，J 低=动能弱）"},
    "BOLL":    {"label": "布林位置",   "family": "技术形态", "unit": "%",  "scale": 1.0,
                "desc": "收盘价在布林带内的位置（0=下轨，100=上轨）；越靠上轨越偏强"},
    "VOL_ABS": {"label": "量能异常",   "family": "量价",     "unit": "x",  "scale": 1.5,
                "desc": "量比（当日量/MA20 量）：放量上涨为正、放量下跌为负、缩量中性"},
    "MOM":     {"label": "动量(20日)",  "family": "动量",     "unit": "%",  "scale": 4.0,
                "desc": "近 20 个交易日累计收益率；越大动量越强"},
    "VOLA":    {"label": "波动率(稳定)", "family": "质量",    "unit": "%",  "scale": 40.0,
                "desc": "近 20 日日收益波动率（年化）；取负向 = 越稳越好（稳定性因子）"},
}
def _equal_weights(dims) -> dict:
    """等权先验：每维 1/n，按 4 位小数收口后把舍入残差补到首位 → 权重和恰为 1。

    2026-10-16 修（verify_quant.py 的「self_learn.权重和≈1」抓出）：
    1/n 对 7 个因子是 0.142857…，而展示/落库口径统一 round(…, 4)，7×0.1429 = 1.0003，
    权重和不再是 1。metalearn.weights_from_ic 内部本就有「精度收口 + 残差补首位」的约定，
    这里对齐同一约定，避免同一件事两套口径。
    注：四支柱（1/4 = 0.25）与 evolve 四维（0.34/0.34/0.14/0.18）本就整除，无此问题。
    """
    w = {d: round(1.0 / len(dims), 4) for d in dims}
    if w:
        k0 = next(iter(w))
        w[k0] = round(w[k0] + round(1.0 - sum(w.values()), 4), 4)
    return w


DEFAULT_W = _equal_weights(TECH_FACTORS)

# ---- 待接入因子（诚实声明，绝不编造、绝不参学）-------------------------------
PENDING_FACTORS = [
    {"key": "PE_PB", "label": "估值 PE/PB 分位", "family": "估值",
     "status": "待接入（需 akshare/联网取真实 PE/PB，本沙箱不可用，不编数据、不参学）"},
    {"key": "ROE", "label": "ROE 质量", "family": "质量",
     "status": "待接入（需 akshare/联网取真实 ROE，本沙箱不可用，不编数据、不参学）"},
    {"key": "GROWTH", "label": "营收/利润成长", "family": "成长",
     "status": "待接入（需 akshare/联网取真实成长数据，本沙箱不可用，不编数据、不参学）"},
    {"key": "SENTI", "label": "论坛情绪/资金流", "family": "情绪",
     "status": "待接入（复用现有论坛情绪/资金流源，待源恢复后接入，本期不参学）"},
]

DDL = """
CREATE TABLE IF NOT EXISTS quant_recipe (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  weights_json TEXT NOT NULL,
  created_at  TEXT
);
"""

_LOCK = threading.Lock()
_STATUS: dict = {"stage": "", "detail": "", "at": ""}
_PANEL_CACHE: dict = {"updated": None, "key": None, "data": None}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _f(x):
    try:
        return float(x)
    except Exception:  # noqa: BLE001
        return None


def _is_ashare(code: str) -> bool:
    """与 pillars._A_SHARE_SQL 同口径：只认真 A 股（剔可转债/ B 股）。"""
    c = str(code or "")
    return (len(c) == 6 and (
        c.startswith(("60", "00", "30", "68", "92", "43", "83", "87"))))


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


# ================================================================ K 线底座
def _read_names(con) -> dict:
    """code → name；con 为 None 时开只读连接（降级用）。"""
    try:
        rows = con.execute("SELECT code, name FROM stock_names").fetchall()
        return {r["code"]: r["name"] for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def _open_db():
    try:
        con = sqlite3.connect(P.DB_PATH)
        con.row_factory = sqlite3.Row
        return con
    except Exception:  # noqa: BLE001
        return None


def _universe_bars(kc_updated=None):
    """合并本地 K 线缓存 → {code: [(date,o,h,l,c,v), ...]}（升序），仅真 A 股且 >= MIN_BARS。

    返回 (bars_map, updated)。bars_map 的 bar 元组与 resonance.load_bars 同构（date, close, high, low, vol 顺序），
    但这里额外保留 open 以便量能口径更准。

    2026-10-29 修复（「量化面板永远算不出来」的真凶）：
      旧实现把全市场 5239 只票的**全历史**逐只读出来常驻内存 —— 实测子进程内存冲到 16.7GB
      被系统直接杀掉，于是 /api/quant 永远停在"计算中"、/api/etf 永远 >60s 超时。
      但因子计算实际只需要「LOOKBACK(65) + BT_DATES(120) + 前向收益(20)」≈ 205 根，
      故此处只取**最近 _SERIES_TAIL 根**（260，留足余量），并优先走 kbase 的批量 OHLCV
      （一次 SQL 代替 5239 次开连接）。内存百 MB 级、耗时从分钟级降到几十秒。
      ⚠️ 口径不变：窗口比最长因子窗口 + 回测期 + 前向期都长，任何因子取值与收益都不受影响。
    """
    kc = P.load_kcache()
    bars_in = (kc.get("bars") or {})
    updated = kc.get("updated") or kc_updated
    sources = [c for c in bars_in.keys() if _is_ashare(c)]   # 只迭代代码（一次 SQL），不逐只读盘
    rows_in = None
    try:
        from viewer import kbase as _kb
        base = _kb.get_base()
        cal = base.distinct_dates()
        min_date = cal[-_SERIES_TAIL] if len(cal) > _SERIES_TAIL else ""
        rows_in = base.ohlcv_bulk(sources, min_date=min_date) or None
    except Exception:  # noqa: BLE001  批量不可用 → 退回逐只读取（慢，但结果一致）
        rows_in = None
    out: dict = {}
    for code in sources:
        if rows_in is not None:
            rows = list(rows_in.get(code) or [])[-_SERIES_TAIL:]
        else:
            v = bars_in.get(code) or []
            rows = []
            for b in v[-_SERIES_TAIL:]:
                try:
                    rows.append((str(b.get("d"))[:10], _f(b.get("o")), _f(b.get("h")),
                                 _f(b.get("l")), _f(b.get("c")), _f(b.get("v"))))
                except Exception:  # noqa: BLE001
                    continue
        rows = [r for r in rows if r[0] and r[4]]
        if len(rows) >= MIN_BARS:
            out[code] = rows          # 批量路径已按 date 升序；逐只路径保持原 sort 语义
    return out, updated


# ================================================================ 因子计算
def _rolling(values, i, k):
    """返回 values[i-k+1 .. i] 的 (mean, std)，不足窗口返回 (None, None)。"""
    if i < k - 1:
        return None, None
    seg = values[i - k + 1:i + 1]
    if any(x is None for x in seg):
        return None, None
    m = sum(seg) / k
    var = sum((x - m) ** 2 for x in seg) / k
    return m, math.sqrt(var)


def _compute_factor_series(rows):
    """对一只票的 bars 序列算 7 个因子的 oriented/main 两路取值，按行索引对齐。

    返回 {factor: {"oriented":[...], "main":[...]}, "idx_date":[date_i]}，
    索引 i < LOOKBACK 或数据无效 → None。oriented 已对齐成「越大越好」。
    """
    n = len(rows)
    closes = [r[4] for r in rows]
    highs = [r[2] for r in rows]
    lows = [r[3] for r in rows]
    vols = [r[5] for r in rows]
    dates = [r[0] for r in rows]

    # 前缀和（MA 快速算）
    cs = [0.0] * (n + 1)
    for i, c in enumerate(closes):
        cs[i + 1] = cs[i] + (c or 0.0)

    def ma(i, k):
        if i < k - 1:
            return None
        return (cs[i + 1] - cs[i + 1 - k]) / k

    # EMA / MACD / KDJ 增量
    ema12 = ema26 = dea = None
    K = 50.0
    D = 50.0
    ema12_a = [None] * n
    ema26_a = [None] * n
    dif_a = [None] * n
    dea_a = [None] * n
    K_a = [None] * n
    D_a = [None] * n
    J_a = [None] * n
    for i in range(n):
        c = closes[i]
        if c is None:
            ema12_a[i] = ema26_a[i] = dif_a[i] = dea_a[i] = K_a[i] = D_a[i] = J_a[i] = None
            continue
        ema12 = c if ema12 is None else (2.0 / 13.0) * c + (11.0 / 13.0) * ema12
        ema26 = c if ema26 is None else (2.0 / 27.0) * c + (25.0 / 27.0) * ema26
        dif = ema12 - ema26
        dea = dif if dea is None else (2.0 / 10.0) * dif + (8.0 / 10.0) * dea
        ema12_a[i], ema26_a[i], dif_a[i], dea_a[i] = ema12, ema26, dif, dea
        # KDJ(9)：RSV → K(2/3,1/3) → D(2/3,1/3) → J=3K-2D
        lo_v = hi_v = None
        if i >= 8:
            seg_lo = [x for x in lows[i - 8:i + 1] if x is not None]
            seg_hi = [x for x in highs[i - 8:i + 1] if x is not None]
            if seg_lo and seg_hi:
                lo_v, hi_v = min(seg_lo), max(seg_hi)
        rsv = 50.0
        if lo_v is not None and hi_v is not None and hi_v > lo_v:
            rsv = (c - lo_v) / (hi_v - lo_v) * 100.0
        K = 2.0 / 3.0 * K + 1.0 / 3.0 * rsv
        D = 2.0 / 3.0 * D + 1.0 / 3.0 * K
        ema12_a[i], ema26_a[i], dif_a[i], dea_a[i] = ema12, ema26, dif, dea
        K_a[i], D_a[i], J_a[i] = K, D, 3.0 * K - 2.0 * D

    # 日收益（BOLL 波动率/带宽用）
    rets = [None] * n
    for i in range(1, n):
        if closes[i] and closes[i - 1]:
            rets[i] = closes[i] / closes[i - 1] - 1.0

    out = {f: {"oriented": [None] * n, "main": [None] * n} for f in TECH_FACTORS}
    idx_date = [None] * n
    for i in range(n):
        if i < LOOKBACK:
            continue
        c = closes[i]
        if c is None or c <= 0:
            continue
        idx_date[i] = dates[i]
        ma5, ma10, ma20, ma60 = ma(i, 5), ma(i, 10), ma(i, 20), ma(i, 60)
        # ① MA_POS：价格/MA20 - 1，加均线排列加权
        if ma20 and ma20 > 0:
            mp = c / ma20 - 1.0
            oriented = mp
            if ma5 and ma10 and ma60:
                oriented += 0.3 * (ma5 / ma20 - 1.0) + 0.2 * (ma10 / ma60 - 1.0)
            out["MA_POS"]["oriented"][i] = oriented
            out["MA_POS"]["main"][i] = mp * 100.0
        # ② MACD：histogram = DIF-DEA 占收盘价比
        if dif_a[i] is not None and dea_a[i] is not None:
            h = (dif_a[i] - dea_a[i]) / c
            out["MACD"]["oriented"][i] = h
            out["MACD"]["main"][i] = h * 100.0
        # ③ KDJ：J 相对 50 的偏离
        if J_a[i] is not None:
            out["KDJ"]["oriented"][i] = (J_a[i] - 50.0) / 50.0
            out["KDJ"]["main"][i] = J_a[i]
        # ④ BOLL：布林带内位置
        mu, sd = _rolling(closes, i, 20)
        if mu is not None and sd is not None:
            up = mu + 2.0 * sd
            dn = mu - 2.0 * sd
            band = up - dn
            pos = (c - dn) / band if band > 1e-9 else 0.5
            out["BOLL"]["oriented"][i] = max(0.0, min(1.0, pos))
            out["BOLL"]["main"][i] = pos * 100.0
        # ⑤ VOL_ABS：量比 × 当日涨跌方向
        vm, _ = _rolling(vols, i, 20)
        if vm and vm > 0 and vols[i] is not None:
            vr = vols[i] / vm
            direction = 1.0 if (rets[i] is not None and rets[i] >= 0) else -1.0
            out["VOL_ABS"]["oriented"][i] = (vr - 1.0) * direction
            out["VOL_ABS"]["main"][i] = vr
        # ⑥ MOM：近 20 日收益
        if i >= 20 and closes[i - 20]:
            mom = c / closes[i - 20] - 1.0
            out["MOM"]["oriented"][i] = mom
            out["MOM"]["main"][i] = mom * 100.0
        # ⑦ VOLA：20 日波动率（年化）→ 取负向（越稳越好）
        rm, rs = _rolling(rets, i, 20)
        if rs is not None:
            ann = rs * math.sqrt(242.0)
            out["VOLA"]["oriented"][i] = -ann
            out["VOLA"]["main"][i] = ann * 100.0
    return {**out, "idx_date": idx_date}


def _disp_score(key: str, oriented):
    """把 oriented 值压到 [-1,1] 区间用于单票全景展示（固定参考尺度）。"""
    if oriented is None:
        return None
    s = oriented / FACTOR_META[key]["scale"]
    return max(-1.0, min(1.0, math.tanh(s)))


def _cs_pct(sorted_vals: list, x):
    """x 在已排序升序列表中的百分位（0~1）：越大越高的 raw → 越高百分位。"""
    if not sorted_vals:
        return 0.5
    return bisect.bisect_right(sorted_vals, x) / len(sorted_vals)


# ================================================================ 因子面板（回测/选股/学习共用）
def _build_panel(codes, bars_map, max_dates=BT_DATES):
    """构建截面因子面板（一次性、较重，结果缓存）。

    返回 dict：
      dates        : 升序信号日列表（去掉末尾不足回测窗口的）
      by_code      : {code: {factor: [oriented_i or None], "date": [date_i]}}
      by_date      : {date: {code: idx}}（该信号日有数据的票）
      fwd          : {w: {code: {date: 前向收益}}}（仅当 idx+w < len 才填）
      series       : {code: factor_series}
    口径：oriented 已对齐成「越大越好」，IC 即 oriented 与未来超额的相关系数。
    """
    series = {}
    by_code = {}
    by_date = {}
    for code in codes:
        rows = bars_map[code]
        fs = _compute_factor_series(rows)
        series[code] = fs
        by_code[code] = {f: fs[f]["oriented"] for f in TECH_FACTORS}
        for i, d in enumerate(fs["idx_date"]):
            if d is None:
                continue
            by_date.setdefault(d, {})[code] = i
    # 取最近 max_dates 个信号日（需要足够票构成截面）
    all_dates = sorted(by_date.keys())
    if len(all_dates) > max_dates:
        all_dates = all_dates[-max_dates:]
    # 过滤掉截面票数 < 30 的稀疏日（IC 无意义）
    dates = [d for d in all_dates if len(by_date[d]) >= 30]
    # 前向收益（T+5/10/20），按各票自身索引取，对齐到信号日
    fwd = {w: {} for w in (5, 10, 20)}
    for code in codes:
        rows = bars_map[code]
        fs = series[code]
        for i, d in enumerate(fs["idx_date"]):
            if d is None or d not in by_date:
                continue
            for w in (5, 10, 20):
                j = i + w
                if j < len(rows) and rows[i][4] and rows[j][4]:
                    fwd[w].setdefault(code, {})[d] = rows[j][4] / rows[i][4] - 1.0
    return {"dates": dates, "by_code": by_code, "by_date": by_date,
            "fwd": fwd, "series": series}


def _panel(codes, bars_map, updated):
    """带缓存的面板构建（按 kcache updated + 宇宙规模 判缓存失效）。"""
    key = (updated, len(codes))
    if _PANEL_CACHE.get("key") == key and _PANEL_CACHE.get("data"):
        return _PANEL_CACHE["data"]
    data = _build_panel(codes, bars_map, BT_DATES)
    _PANEL_CACHE["key"] = key
    _PANEL_CACHE["data"] = data
    return data


def _ic_by_factor(panel, w=5):
    """各因子在 T+w 上的截面 IC（pooled pearson）与样本数，不足 → (None, 0)。"""
    ic = {}
    n_by = {}
    for f in TECH_FACTORS:
        xs, ys = [], []
        for code, dmap in panel["fwd"][w].items():
            ori = panel["by_code"][code][f]
            for d, fr in dmap.items():
                if d not in panel["by_date"]:
                    continue
                idx = panel["by_date"][d][code]
                ov = ori[idx] if idx < len(ori) else None
                if ov is None or fr is None:
                    continue
                xs.append(ov)
                ys.append(fr)
        n = len(xs)
        n_by[f] = n
        ic[f] = pearson(xs, ys) if n >= MIN_IC_N else None
    return ic, n_by


def _ic_ir_by_factor(panel, w=5):
    """各因子 IC 与 IR（逐日截面 IC 的均值/标准差），不足 → (None, None)。"""
    # 逐日截面 IC
    daily = {f: [] for f in TECH_FACTORS}
    for d in panel["dates"]:
        codes_i = panel["by_date"][d]
        for f in TECH_FACTORS:
            xs, ys = [], []
            for code, idx in codes_i.items():
                ori = panel["by_code"][code][f]
                ov = ori[idx] if idx < len(ori) else None
                fr = panel["fwd"][w].get(code, {}).get(d)
                if ov is None or fr is None:
                    continue
                xs.append(ov)
                ys.append(fr)
            if len(xs) >= MIN_IC_N:
                daily[f].append(pearson(xs, ys))
    out_ic, out_ir = {}, {}
    for f in TECH_FACTORS:
        arr = daily[f]
        if len(arr) >= 3:
            m = sum(arr) / len(arr)
            sd = math.sqrt(sum((x - m) ** 2 for x in arr) / len(arr)) if len(arr) > 1 else 0.0
            out_ic[f] = round(m, 4)
            out_ir[f] = round(m / sd, 4) if sd > 1e-9 else None
        else:
            out_ic[f] = None
            out_ir[f] = None
    return out_ic, out_ir


def _neutralize_panel(raw: dict, neutralize: dict) -> None:
    """原地对 raw[code][factor] 做 行业/市值 中性化（取残差），修改 raw 本身。

    neutralize: {code: {"industry": str|None, "mcap": float|None}}。缺失 code 或字段为 None → 该票跳过。
    方法（P1-18）：两步残差化——① 市值线性去趋势（因子值对 mcap 回归取残差）；② 行业均值中心化
    （减去同行业均值），避免复合分只是「大盘 β / 行业暴露」的镜像。因子有效票 < 10 时跳过该因子
    中性化，避免小样本过拟合。无可中性的环境（neutralize=None）调用方直接不传，行为自然回退。
    """
    for f in TECH_FACTORS:
        xs = [(c, raw[c][f]) for c in raw if f in raw[c]]
        if len(xs) < 10:
            continue
        # ① 市值线性去趋势
        mc = [(c, (neutralize.get(c) or {}).get("mcap")) for c, _ in xs]
        pairs = [(y, m) for (c, y), (_, m) in zip(xs, mc) if m is not None]
        if len(pairs) >= 10:
            ms = [m for _, m in pairs]; ys = [y for y, _ in pairs]
            mmean = sum(ms) / len(ms); ymean = sum(ys) / len(ys)
            num = sum((m - mmean) * (y - ymean) for m, y in pairs)
            den = sum((m - mmean) ** 2 for m in ms)
            beta = num / den if den > 1e-12 else 0.0
            for c, y in xs:
                m = (neutralize.get(c) or {}).get("mcap")
                if m is not None:
                    raw[c][f] = y - beta * (m - mmean)
        # ② 行业均值中心化
        by_ind = {}
        for c, y in xs:
            ind = (neutralize.get(c) or {}).get("industry")
            if ind:
                by_ind.setdefault(ind, []).append((c, y))
        for ind, lst in by_ind.items():
            if len(lst) >= 3:
                im = sum(y for _, y in lst) / len(lst)
                for c, y in lst:
                    raw[c][f] = y - im


def _bh_fdr(pvals: list, alpha: float = 0.05):
    """Benjamini-Hochberg FDR 多重检验校正（P1-21）。

    输入 p 值列表，返回 (rejected: set of 下标, qvals: list)。
    100 个策略/因子同时做样本外显著性检验时，朴素 0.05 阈值会把约 5 个假阳性当真——BH 按 p 值升序
    逐步放宽阈值（阈值 = alpha * rank / m），只放行真正通过多重校正的项，抑制「挑出一堆伪显著」。
    """
    m = len(pvals)
    if m == 0:
        return set(), []
    order = sorted(range(m), key=lambda i: pvals[i])
    qvals = [1.0] * m
    prev = 1.0
    rejected = set()
    for rank, i in enumerate(order, 1):
        thr = alpha * rank / m
        q = min(prev, (pvals[i] * m / rank) if rank else 1.0)
        qvals[i] = q
        prev = q
        if pvals[i] <= thr:
            rejected.add(i)
    return rejected, qvals


def _composite_at(panel, date, weights, neutralize=None):
    """在某信号日，用 weights 给各票算截面复合分。

    P1-15 整改：因子先做**截面 z-score 标准化**再加权——同一信号日不同票的 raw oriented 值
    量纲/分布不同，直接加权会被大波动因子主导。改为对每只因子在当日截面做 z-score
    （(x-μ)/σ，σ≈0 退化为 0），使各因子在同一把尺子上可比。
    P1-18 整改：neutralize 传入 {code:{"industry":..,"mcap":..}} 时，先对因子值做行业/市值中性化
    （取残差，见 _neutralize_panel）再标准化加权，避免复合分只是「大盘/行业 β」镜像。
    neutralize=None（默认可无中性数据环境）时跳过中性化，行为回退到纯截面标准化，不报错。
    新参数 neutralize 有默认值，旧调用方（_oos_panel_metrics / _walkforward_oos）签名不变。
    """
    codes_i = panel["by_date"].get(date)
    if not codes_i:
        return {}
    raw = {}
    for code, idx in codes_i.items():
        vals = {}
        for f in TECH_FACTORS:
            ori = panel["by_code"][code][f]
            ov = ori[idx] if idx < len(ori) else None
            if ov is None or weights.get(f) is None:
                continue
            vals[f] = float(ov)
        if vals:
            raw[code] = vals
    if not raw:
        return {}
    if neutralize:
        _neutralize_panel(raw, neutralize)
    # P1-15：截面 z-score 标准化（每因子当日截面）
    for f in TECH_FACTORS:
        xs = [v[f] for v in raw.values() if f in v]
        if not xs:
            continue
        m = sum(xs) / len(xs)
        var = sum((x - m) ** 2 for x in xs) / len(xs)
        sd = math.sqrt(var) if var > 1e-12 else 0.0
        for v in raw.values():
            if f in v:
                v[f] = ((v[f] - m) / sd) if sd > 0 else 0.0
    wsum = sum(weights[f] for f in TECH_FACTORS if weights.get(f) is not None) or 1.0
    scores = {}
    for code, v in raw.items():
        s = sum(weights[f] * v[f] for f in v)
        scores[code] = s / wsum
    return scores


def _top_n_codes(scores: dict, top_n: int):
    items = sorted(scores.items(), key=lambda kv: -kv[1])
    return [c for c, _ in items[:top_n]]


def _oos_panel_metrics(panel, top_n, weights_fn, name=""):
    """面板外样回溯（walk-forward 同构）：每期用 weights_fn(panel, date) 给出的权重打分→取前 N→下期收益。

    返回 {n_days, recipe_mean, recipe_std, recipe_sharpe, recipe_win, equal_mean, equal_win, beat}。
    诚实口径：这是「历史面板回溯」而非真实选股复盘；权重固定（如菜谱）时即该配比的历史表现。
    """
    recipe_rets, equal_rets = [], []
    for d in panel["dates"]:
        w = weights_fn(panel, d)
        sc = _composite_at(panel, d, w)
        if not sc:
            continue
        top = _top_n_codes(sc, top_n)
        if len(top) < min(top_n, 3):
            continue
        eq_w = {f: DEFAULT_W[f] for f in TECH_FACTORS}
        eq_sc = _composite_at(panel, d, eq_w)
        eq_top = _top_n_codes(eq_sc, top_n)
        if len(eq_top) < 3:
            continue
        rec = [panel["fwd"][5].get(c, {}).get(d) for c in top]
        eqv = [panel["fwd"][5].get(c, {}).get(d) for c in eq_top]
        rec = [x for x in rec if x is not None]
        eqv = [x for x in eqv if x is not None]
        if not rec or not eqv:
            continue
        recipe_rets.append(sum(rec) / len(rec))
        equal_rets.append(sum(eqv) / len(eqv))
    return _summarize_oos(recipe_rets, equal_rets)


def _summarize_oos(recipe_rets, equal_rets):
    n = len(recipe_rets)
    if n < 1:
        return {"n_days": 0, "recipe_mean": None, "recipe_std": None,
                "recipe_sharpe": None, "recipe_win": None,
                "equal_mean": None, "equal_win": None, "beat": None}
    rm = sum(recipe_rets) / n
    em = sum(equal_rets) / n
    rs = math.sqrt(sum((x - rm) ** 2 for x in recipe_rets) / n)
    sd = math.sqrt(sum((x - em) ** 2 for x in equal_rets) / n)
    win_r = sum(1 for x in recipe_rets if x > 0) / n
    ewin = sum(1 for x in equal_rets if x > 0) / n
    beat = sum(1 for a, b in zip(recipe_rets, equal_rets) if a > b) / n
    return {"n_days": n,
            "recipe_mean": round(rm, 5), "recipe_std": round(rs, 5),
            "recipe_sharpe": round(rm / rs, 3) if rs > 1e-9 else None,
            "recipe_win": round(win_r, 3),
            "equal_mean": round(em, 5), "equal_win": round(ewin, 3),
            "beat": round(beat, 3)}


def _walkforward_oos(panel, top_n, default_w):
    """真·样本外验证（mirrors pillars._sl_oos）：每测试日用前期 IC 学权重，比较加权 vs 等权前 N。

    返回 {n, win_rate, mean_diff, passed}。可比期数不足 OOS_MIN 或胜率不达标 → 诚实未通过。
    """
    dates = panel["dates"]
    comps = []
    for k in range(PANEL_STEP, len(dates), PANEL_STEP):
        d_test = dates[k]
        train_dates = dates[:k]
        # 前期 IC（pooled）
        ic, n_by = {}, {}
        for f in TECH_FACTORS:
            xs, ys = [], []
            for code, dmap in panel["fwd"][5].items():
                for d, fr in dmap.items():
                    if d not in train_dates:
                        continue
                    idx = panel["by_date"][d][code]
                    ori = panel["by_code"][code][f]
                    ov = ori[idx] if idx < len(ori) else None
                    if ov is None or fr is None:
                        continue
                    xs.append(ov)
                    ys.append(fr)
            n_by[f] = len(xs)
            ic[f] = pearson(xs, ys) if len(xs) >= MIN_IC_N else None
        w = weights_from_ic(ic, n_by, list(TECH_FACTORS), default_w)[0]
        sc = _composite_at(panel, d_test, w)
        eq_sc = _composite_at(panel, d_test, {f: default_w[f] for f in TECH_FACTORS})
        top = _top_n_codes(sc, top_n)
        eq_top = _top_n_codes(eq_sc, top_n)
        if len(top) < 3 or len(eq_top) < 3:
            continue
        rec = [panel["fwd"][5].get(c, {}).get(d_test) for c in top]
        eqv = [panel["fwd"][5].get(c, {}).get(d_test) for c in eq_top]
        rec = [x for x in rec if x is not None]
        eqv = [x for x in eqv if x is not None]
        if not rec or not eqv:
            continue
        comps.append((sum(rec) / len(rec), sum(eqv) / len(eqv)))
    n = len(comps)
    if n == 0:
        return {"n": 0, "win_rate": None, "mean_diff": None, "passed": False}
    win = sum(1 for wm, em in comps if wm > em)
    wr = win / n
    md = sum(wm - em for wm, em in comps) / n
    passed = (n >= OOS_MIN) and (wr >= OOS_WIN) and (md > 0)
    return {"n": n, "win_rate": round(wr, 3), "mean_diff": round(md, 5), "passed": passed}


# ================================================================ 对外接口
def factor_panorama(code: str, con=None) -> dict:
    """单票因子全景（M1）：7 个技术因子打分（oriented → [-1,1] 展示分 + 原始值）。

    口径：单票无截面，展示分用固定参考尺度 tanh 压到 [-1,1]（见 FACTOR_META.scale），
    纯展示用；真正用于选股/学习的是截面百分位（见 multi_factor_select / quant_self_learn）。
    """
    bars_map, _ = _universe_bars()
    rows = bars_map.get(code)
    if not rows:
        return {"ok": False, "code": code, "has_data": False,
                "reason": "本地 K 线无该票或不足 %d 根" % MIN_BARS}
    fs = _compute_factor_series(rows)
    name = ""
    if con is not None:
        name = _read_names(con).get(code, "")
    # 取最近一个有效索引（末根 bar 若停牌/缺口则向前找）
    last_i = len(rows) - 1
    while last_i >= 0 and fs["idx_date"][last_i] is None:
        last_i -= 1
    factors = []
    for f in TECH_FACTORS:
        ori = fs[f]["oriented"][last_i]
        factors.append({
            "key": f, "label": FACTOR_META[f]["label"], "family": FACTOR_META[f]["family"],
            "unit": FACTOR_META[f]["unit"], "desc": FACTOR_META[f]["desc"],
            "raw": (round(fs[f]["main"][last_i], 4) if fs[f]["main"][last_i] is not None else None),
            "score": _disp_score(f, ori),
        })
    return {"ok": True, "code": code, "name": name, "has_data": True,
            "data_date": fs["idx_date"][last_i] or "", "factors": factors}


def factor_backtest(con=None) -> dict:
    """各因子 IC / IR / 衰减（M3）：T+5 主轴 + T+10/T+20 衰减；样本不足 IC=None。"""
    bars_map, updated = _universe_bars()
    codes = list(bars_map.keys())
    if len(codes) < 30:
        return {"ok": False, "reason": "可计算票不足 30 只（本地 K 线覆盖有限）",
                "n_universe": len(codes)}
    panel = _panel(codes, bars_map, updated)
    ic5, n5 = _ic_by_factor(panel, 5)
    ic10, n10 = _ic_by_factor(panel, 10)
    ic20, n20 = _ic_by_factor(panel, 20)
    ir5, _ = _ic_ir_by_factor(panel, 5)
    per = []
    for f in TECH_FACTORS:
        per.append({
            "key": f, "label": FACTOR_META[f]["label"], "family": FACTOR_META[f]["family"],
            "ic5": (round(ic5[f], 4) if ic5[f] is not None else None),
            "ir5": (round(ir5[f], 4) if ir5[f] is not None else None),
            "ic10": (round(ic10[f], 4) if ic10[f] is not None else None),
            "ic20": (round(ic20[f], 4) if ic20[f] is not None else None),
            "n5": n5[f], "n10": n10[f], "n20": n20[f],
            "status": ("OK" if ic5[f] is not None else "样本不足（IC=None，不参学）"),
        })
    return {"ok": True, "n_universe": len(codes), "last_date": panel["dates"][-1] if panel["dates"] else None,
            "n_dates": len(panel["dates"]),
            "factors": per,
            "pending": PENDING_FACTORS}


def multi_factor_select(top_n: int = TOP_N_SELECT, con=None) -> dict:
    """截面多因子选股（M2）：用 metalearn 默认引擎（对可学因子算 IC→权重）给全宇宙打分出清单。

    不足样本时诚实等权（weights_from_ic 内部自动退回 default_w）。
    """
    bars_map, updated = _universe_bars()
    codes = list(bars_map.keys())
    names = _read_names(con) if con is not None else {}
    if len(codes) < 30:
        return {"ok": False, "reason": "可计算票不足 30 只", "n_universe": len(codes)}
    panel = _panel(codes, bars_map, updated)
    ic5, n5 = _ic_by_factor(panel, 5)
    w, shrunk, learned, has_shrunk, note = weights_from_ic(
        ic5, n5, list(TECH_FACTORS), DEFAULT_W)
    last = panel["dates"][-1] if panel["dates"] else None
    scores = _composite_at(panel, last, w) if last else {}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    items = []
    for rank, (code, sc) in enumerate(ranked[:top_n], 1):
        fac_bits = []
        for f in TECH_FACTORS:
            ori = panel["by_code"][code][f]
            idx = panel["by_date"][last][code]
            ov = ori[idx] if idx < len(ori) else None
            if ov is not None:
                fac_bits.append("%s %+.2f" % (FACTOR_META[f]["label"], ov))
        items.append({
            "rank": rank, "symbol": code, "name": names.get(code, ""),
            "score": round(sc, 4),
            "reason": "截面复合分；权重（数据学出）：%s" % " · ".join(
                "%s %.0f%%" % (FACTOR_META[f]["label"], w[f] * 100) for f in TECH_FACTORS),
            "detail": {"weights": {f: round(w[f], 4) for f in TECH_FACTORS},
                       "factor_pct": fac_bits[:6]},
        })
    return {"ok": True, "top_n": top_n, "n_universe": len(codes), "data_date": last,
            "weights": {f: round(w[f], 4) for f in TECH_FACTORS},
            "weights_effective": has_shrunk,
            "weight_stage": ("收缩生效中" if has_shrunk else "等权运行中（样本不足）"),
            "note": note,
            "items": items,
            "pending_inactive": [p["label"] for p in PENDING_FACTORS]}


# ================================================================ 菜谱预设（M4）
def recipe_list(con: sqlite3.Connection) -> list:
    ensure_tables(con)
    rows = con.execute("SELECT id, name, weights_json, created_at FROM quant_recipe "
                      "ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        try:
            w = json.loads(r["weights_json"] or "{}")
        except Exception:  # noqa: BLE001
            w = {}
        out.append({"id": r["id"], "name": r["name"], "weights": w, "created_at": r["created_at"]})
    return out


def recipe_save(con: sqlite3.Connection, name: str, weights: dict) -> dict:
    """进哥「私家配比」存盘：weights = {因子key: 权重}（可只填部分，其余按 0 处理）。"""
    ensure_tables(con)
    if not name or not isinstance(weights, dict):
        return {"ok": False, "error": "name/weights 非法"}
    # 只保留已知技术因子，归一化到和 1（作为引擎的先验 prior 输入）
    w = {f: float(weights.get(f, 0.0) or 0.0) for f in TECH_FACTORS}
    tot = sum(w.values())
    if tot <= 0:
        return {"ok": False, "error": "权重和必须 > 0"}
    w = {f: round(w[f] / tot, 4) for f in TECH_FACTORS}
    con.execute("INSERT INTO quant_recipe(name, weights_json, created_at) VALUES(?,?,?)",
                (name, json.dumps(w, ensure_ascii=False), _now()))
    con.commit()
    return {"ok": True, "name": name, "weights": w}


def recipe_apply(con: sqlite3.Connection, recipe_id: int) -> dict:
    """应用菜谱：把用户配比作为引擎先验（prior）输入，并**强制报 OOS 外样 Sharpe/胜率**。

    不单给回测漂亮数字：用该配比在历史上做面板外样回溯（walk-forward 同构），
    报 recipe 与等权两版的均值/Sharpe/胜率，诚实对比。
    """
    ensure_tables(con)
    row = con.execute("SELECT id, name, weights_json FROM quant_recipe WHERE id=?",
                      (recipe_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "菜谱不存在"}
    prior = {f: 0.0 for f in TECH_FACTORS}
    try:
        saved = json.loads(row["weights_json"] or "{}")
    except Exception:  # noqa: BLE001
        saved = {}
    for f, v in saved.items():
        if f in prior:
            prior[f] = float(v)
    tot = sum(prior.values())
    if tot <= 0:
        return {"ok": False, "error": "菜谱权重和 <= 0"}
    prior = {f: prior[f] / tot for f in TECH_FACTORS}

    bars_map, updated = _universe_bars()
    codes = list(bars_map.keys())
    if len(codes) < 30:
        return {"ok": False, "reason": "可计算票不足 30 只", "n_universe": len(codes),
                "recipe": {"id": row["id"], "name": row["name"], "weights": prior}}
    panel = _panel(codes, bars_map, updated)
    # 用菜谱作为先验 → 学出最终权重（IC 收缩 + 先验混合 + 下限）
    ic5, n5 = _ic_by_factor(panel, 5)
    w, _s, _l, has_shrunk, note = weights_from_ic(ic5, n5, list(TECH_FACTORS), prior)
    # 强制 OOS：面板外样回溯
    oos = _oos_panel_metrics(panel, TOP_N_SELECT, lambda p, d: w)
    # 当前截面清单（用菜谱学出的权重）
    last = panel["dates"][-1] if panel["dates"] else None
    scores = _composite_at(panel, last, w) if last else {}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:TOP_N_SELECT]
    names = _read_names(con)
    items = [{"rank": i, "symbol": c, "name": names.get(c, ""), "score": round(s, 4)}
             for i, (c, s) in enumerate(ranked, 1)]
    return {"ok": True, "recipe": {"id": row["id"], "name": row["name"], "weights": prior},
            "final_weights": {f: round(w[f], 4) for f in TECH_FACTORS},
            "weights_effective": has_shrunk, "note": note,
            "oos": oos, "items": items, "data_date": last, "top_n": TOP_N_SELECT,
            "oos_warning": ("样本不足或外样未跑出正超额：菜谱仅作先验参考，不构成收益承诺"
                            if (oos.get("n_days") or 0) < OOS_MIN else "")}


# ================================================================ 量化因子的自我学习与成长（M5）
def quant_self_learn_data(con, td: str) -> dict:
    """实例化 metalearn 于因子候选：产出 weight_stage（三态）/ weights / oos / per_factor / note。

    学习信号 = 因子 IC（由本地 K 线历史回放算出，与历史回测同口径）；
    样本外验证 = walk-forward（每期用前期 IC 学权重→打分→下期收益），非真实选股复盘。
    三态徽章逻辑照抄 pillars 的 honesty 逻辑：等权运行中 / 收缩生效中 / 样本外验证通过。
    """
    bars_map, updated = _universe_bars()
    codes = list(bars_map.keys())
    if len(codes) < 30:
        return {"ok": False, "reason": "可计算票不足 30 只", "n_universe": len(codes)}
    panel = _panel(codes, bars_map, updated)
    ic5, n5 = _ic_by_factor(panel, 5)
    w, shrunk, learned, has_shrunk, wnote = weights_from_ic(
        ic5, n5, list(TECH_FACTORS), DEFAULT_W)
    oos = _walkforward_oos(panel, TOP_N_SELECT, DEFAULT_W)
    fresh, fresh_note = (True, "")
    samples_t5 = sum(n5.values())
    if oos["passed"]:
        weight_stage = "样本外验证通过（权重生效）"
        use_w = w
    elif has_shrunk:
        weight_stage = "收缩生效中（样本 %d，权重靠近先验）" % samples_t5
        use_w = w
    else:
        weight_stage = "等权运行中（样本 %d，尚不足以学习）" % samples_t5
        use_w = w
    oos_failed = has_shrunk and (oos["n"] or 0) >= OOS_MIN and not oos["passed"]
    if oos_failed:
        weight_stage = "等权运行中（未通过样本外验证 → 本期等权）"
        use_w = dict(DEFAULT_W)

    names = _read_names(con) if con is not None else {}
    last = panel["dates"][-1] if panel["dates"] else None
    scores = _composite_at(panel, last, use_w) if last else {}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:TOP_N_SELECT]
    items = []
    for i, (code, sc) in enumerate(ranked, 1):
        items.append({"rank": i, "symbol": code, "name": names.get(code, ""),
                      "score": round(sc, 4), "reason": "因子截面复合分（权重=%s）" % weight_stage,
                      "detail": {"weights": {f: round(use_w[f], 4) for f in TECH_FACTORS}}})

    per_factor = {}
    for f in TECH_FACTORS:
        per_factor[f] = {"label": FACTOR_META[f]["label"], "family": FACTOR_META[f]["family"],
                         "ic5": (round(ic5[f], 4) if ic5[f] is not None else None),
                         "n5": n5[f],
                         "shrunk": (round(shrunk[f], 4) if shrunk.get(f) is not None else None)}

    note = ("学习口径：因子 IC 由本地 K 线历史回放（与历史回测同口径，跑赢截面等权才算有效）算出，"
            "作为权重学习的依据；权重数学走 metalearn 共享引擎（IC 收缩 + 先验混合 + 单维下限）。"
            "主轴=T+5。样本外验证用 walk-forward（每期用前期 IC 学权重→打分→下期收益），非真实选股复盘。"
            "%s。各因子逐期样本：%s"
            % (("样本外验证通过（%d 期可比，加权方胜率 %.0f%%，均值差 %s）"
                % (oos["n"], oos["win_rate"] * 100, "%+.4f" % oos["mean_diff"]))
               if oos["passed"]
               else ("样本 %d（T+5 主轴）远不足可比期门槛 %d → 本期等权"
                     % (samples_t5, OOS_MIN))
               if (oos["n"] or 0) < OOS_MIN
               else ("未通过样本外验证（%d 期可比，加权方胜率 %.0f%%）→ 本期等权"
                     % (oos["n"], oos["win_rate"] * 100)),
               " / ".join("%s n=%d" % (FACTOR_META[f]["label"], n5[f]) for f in TECH_FACTORS)))
    if not fresh:
        note += "（注：%s）" % fresh_note

    state_json = {
        "weights": {f: round(use_w[f], 4) for f in TECH_FACTORS},
        "weight_mode": wnote,
        "weight_stage": weight_stage,
        "weights_effective": (weight_stage.startswith("样本外验证通过")
                              or weight_stage.startswith("收缩生效中")),
        "oos": oos,
        "samples_total": samples_t5,
        "ic_by_factor": {f: (round(ic5[f], 4) if ic5[f] is not None else None) for f in TECH_FACTORS},
        "shrunk_ic": {f: (round(shrunk[f], 4) if shrunk.get(f) is not None else None) for f in TECH_FACTORS},
        "per_factor": per_factor,
        "n_universe": len(codes),
        "data_date": last,
        "kcache_fresh": fresh,
    }
    return {"ok": True, "items": items, "note": note, "state_json": state_json, "td": td}


def persist_self_learn(con: sqlite3.Connection, td: str) -> dict:
    """算 + 落库：复用现有 pillar_run / pillar_item 表，pillar='quant_self' 区分（不新建表、不动 schema）。"""
    ensure_tables(con)
    sl = quant_self_learn_data(con, td)
    if not sl.get("ok"):
        return sl
    state = sl["state_json"]
    con.execute("DELETE FROM pillar_run WHERE run_date=? AND pillar='quant_self'", (td,))
    con.execute("DELETE FROM pillar_item WHERE run_date=? AND pillar='quant_self'", (td,))
    con.execute("INSERT INTO pillar_run(run_date,pillar,status,title,summary,state_json,"
                "note,engine_ver,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (td, "quant_self", "ok", "量化因子的自我学习与成长", sl["note"],
                 json.dumps(state, ensure_ascii=False), sl["note"], ENGINE_VER, _now()))
    for i, it in enumerate(sl["items"], 1):
        con.execute("INSERT OR REPLACE INTO pillar_item(run_date,pillar,symbol,name,rank,"
                    "score,reason,detail_json) VALUES(?,?,?,?,?,?,?,?)",
                    (td, "quant_self", it["symbol"], it.get("name") or it["symbol"], i,
                     it["score"], it["reason"],
                     json.dumps(it.get("detail") or {}, ensure_ascii=False)))
    con.commit()
    return sl


def run(con: sqlite3.Connection, td: str | None = None, force: bool = False) -> dict:
    """M5 落库入口（POST /api/quant/run）：重算自我学习并落库。失败不崩主流程。"""
    with _LOCK:
        t0 = time.time()
        try:
            _STATUS.update(stage="载入K线底座", detail="", at=_now())
            bars_map, _ = _universe_bars()
            codes = list(bars_map.keys())
            if not codes:
                return {"ok": False, "error": "本地 K 线缓存为空"}
            # 2026-10-19 修复：bars_map 此处为 tuple 形态 (date,o,h,l,c,v)，
            # predictor._trade_date_of 期望 dict 形态（v[-1].get("d")）→ 必抛
            # 'tuple' object has no attribute 'get'，导致重新学习 100% 失败。
            # 改为 tuple 安全的同口径实现：取最后一根 K 线日期的众数。
            if not td:
                try:
                    from collections import Counter as _Cnt
                    _cnt = _Cnt()
                    for _rows in bars_map.values():
                        if _rows:
                            _cnt[str(_rows[-1][0])[:10]] += 1
                    td = _cnt.most_common(1)[0][0] if _cnt else None
                except Exception:  # noqa: BLE001
                    td = None
            if not td:
                td = _today()
            if not force:
                row = con.execute("SELECT run_date FROM pillar_run WHERE run_date=? AND pillar='quant_self'",
                                  (td,)).fetchone()
                if row:
                    return {"ok": True, "run_date": td, "cached": True}
            _STATUS.update(stage="学习因子权重+落库", detail=td, at=_now())
            sl = persist_self_learn(con, td)
            _STATUS.update(stage="完成", detail="", at=_now())
            return {"ok": True, "run_date": td, "engine_ver": ENGINE_VER,
                    "elapsed": round(time.time() - t0, 1), **sl}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}


# ================================================================ 只读 payload / status
def payload(con: sqlite3.Connection) -> dict:
    ensure_tables(con)
    # 最新一期自我学习
    row = con.execute("SELECT MAX(run_date) d FROM pillar_run WHERE pillar='quant_self'").fetchone()
    bd = (row and row["d"]) or None
    self_learn = None
    select_items = []
    if bd:
        r = con.execute("SELECT state_json, note FROM pillar_run WHERE run_date=? AND pillar='quant_self'",
                        (bd,)).fetchone()
        try:
            st = json.loads(r["state_json"] or "{}") if r else {}
        except Exception:  # noqa: BLE001
            st = {}
        items = [dict(x) for x in con.execute(
            "SELECT symbol, name, rank, score, reason, detail_json FROM pillar_item "
            "WHERE run_date=? AND pillar='quant_self' ORDER BY rank", (bd,))]
        for it in items:
            try:
                it["detail"] = json.loads(it["detail_json"] or "{}")
            except Exception:  # noqa: BLE001
                it["detail"] = {}
        self_learn = {"run_date": bd, "state": st, "items": items,
                      "note": st.get("note") or (r["note"] if r else "")}
    # 多因子选股（实时算，复用面板缓存）
    try:
        sel = multi_factor_select(con=con)
    except Exception as e:  # noqa: BLE001
        sel = {"ok": False, "error": str(e)[:200]}
    # 因子回测
    try:
        bt = factor_backtest(con=con)
    except Exception as e:  # noqa: BLE001
        bt = {"ok": False, "error": str(e)[:200]}
    # 菜谱
    try:
        recipes = recipe_list(con)
    except Exception:  # noqa: BLE001
        recipes = []
    caliber = [
        "口径1 · 全部技术因子**纯本地 K 线离线算**（predictor.load_kcache），不依赖同花顺、不硬联网。",
        "口径2 · 权重数学**唯一真源 = metalearn.weights_from_ic**（与 E 引擎自加权、evolve 自我进化共用）："
        "IC 收缩 → 只取正 → 归一 → 先验混合 → 单维下限 → 精度收口。禁止在此再抄第三套。",
        "口径3 · 基本面（PE/PB/ROE/成长）与情绪/资金因子**需 akshare/联网，本沙箱不可用**——"
        "只声明维度、标「待接入」，绝不编造数值、绝不参学。",
        "口径4 · 样本不足（IC 样本 < %d 或全 ≤0）→ 诚实退回等权先验，徽章标「等权运行中」，绝不填假权重；"
        "OOS 未通过诚实退回等权。" % MIN_IC_N,
        "口径5 · 因子按家族（动量/技术形态/量价/质量）先分组降维展示，再在家族内/间用 IC 权重融合；"
        "**不枚举 2^100 个子集**——让数据自动淘汰弱因子（IC 不足/为负→权重塌到下限甚至 0）。",
        "口径6 · 自我学习板的「学习信号」= 因子 IC（本地 K 线历史回放，与历史回测同口径）；"
        "「样本外验证」= walk-forward（每期用前期 IC 学权重→打分→下期收益），非真实选股复盘。",
        "口径7 · 截面样本过滤：单期截面票数 < 30 的信号日不参与 IC（避免稀疏日噪声）；"
        "回测/学习最多使用最近 %d 个交易日（控成本）。" % BT_DATES,
    ]
    return {"ok": True, "engine_ver": ENGINE_VER, "build_date": bd,
            "self_learn": self_learn, "select": sel, "backtest": bt,
            "recipes": recipes, "pending_factors": PENDING_FACTORS,
            "caliber": caliber,
            "note": "量化板块是继「每日预测（四支柱）」「博主共振」之后的第三维信号源；"
                    "其自我学习板与另两块对称（都在长「该信谁、给多少权重」）。生成由后端任务完成，展示层只读。"}


def status_payload(con: sqlite3.Connection) -> dict:
    ensure_tables(con)
    return {"engine_ver": ENGINE_VER, "stage": _STATUS.get("stage"),
            "detail": _STATUS.get("detail"), "at": _STATUS.get("at"),
            "today": _today(),
            "runs": [{"run_date": r["run_date"], "items": r["n"]} for r in con.execute(
                "SELECT run_date, COUNT(*) n FROM pillar_item WHERE pillar='quant_self' "
                "GROUP BY run_date ORDER BY run_date DESC LIMIT 8")]}
