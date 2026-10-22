# -*- coding: utf-8 -*-
"""ETF 基金板块（etf-v1.0，2026-10-17 进哥需求落地）。

定位（架构红线，与 quant.py / resonance.py / pillars.py 一致）：本模块只在**后端批量任务 / 请求线程只读**里跑，
查看器只读本模块产出的 payload（左 A / 右 B 两栏分离）。

两栏对称布局（对应需求「左-右对称两列，A/B 必须分开、绝不混叠或上下堆叠」）：
  A  量化多因子中的 ETF 候选 —— 把 ETF 当作量化多因子候选：复用 quant 的 7 个技术因子
      （纯本地 K 线离线算）+ metalearn 权重（唯一真源 weights_from_ic）+ 样本外验证；
      展示每个 ETF 的因子分与一个诚实三态徽章（等权运行中 / 收缩生效中 / 样本外验证通过）。
  B  ETF 专属投顾维度        —— 6 个 ETF 专属维度（份额净申赎/资金流、跟踪指数与成分、溢价率/折溢价、
      规模、费率、跟踪误差）。资金流/行情与跟踪指数**已接入 Sina 实时数据**（etf_advisory_cache.json），
      溢价率/规模/费率/跟踪误差需东财（本沙箱不可用）诚实标「待接入」，绝不编造数值、绝不参学。

诚实口径（写进 payload.note，前端原样展示）：
  - 全部技术因子**纯本地 K 线离线算**（predictor.load_kcache），不依赖同花顺、不硬联网。
  - 当前 predict_kcache.json 有 0 只 ETF 代码 → A 的候选池为空 → A 返回 [] 候选 + 提示
    「ETF 日 K 待接入，候选池为空」；打分逻辑**完全接好线**（对宇宙跑 multi_factor_select 式打分），
    一旦 ETF 日 K 接入即自动点亮，无需改代码。
  - B 的 6 个维度：**资金流/行情/跟踪指数已接入 Sina 实时数据**（离线读 etf_advisory_cache.json，由
    etf_advisory_data.py 经 akshare 打通并沉淀）；溢价率/规模/费率/跟踪误差需东财（本沙箱不可用）
    ——诚实标「待接入」，绝不编造数值、绝不参学。
  - 权重数学**唯一真源 = metalearn.weights_from_ic**（与 quant / E 引擎自加权 / evolve 自我进化共用），
    禁止在此再抄第二套 IC/收缩/先验公式。
  - 样本不足（IC 样本 < MIN_IC_N 或全 ≤0）→ 诚实退回等权先验，徽章标「等权运行中」；
    OOS 未通过诚实退回等权，绝不填假权重。
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime

import predictor as P          # viewer/ 同 resonance.py / pillars.py / quant.py 的依赖方式
import quant                  # 复用其 7 技术因子计算与面板编排（不重抄因子数学）
from quant import (           # 直接复用 quant 的因子常量与等权先验（不重抄）
    TECH_FACTORS, FACTOR_META, DEFAULT_W,
)
from metalearn import (        # 权重学习唯一真源：禁止再抄第二套
    weights_from_ic, pearson, MIN_IC_N, IC_SHRINK_K, W_PRIOR_MIX, W_FLOOR,
)

# ETF 目标宇宙（与 etf_data 同源；B 栏宇宙解耦 kcache 用，A2 修复）。
# 懒导入：etf_data 在 ROOT，且导入会触发 strategy_sim 兜底，故仅在 etf.py 内按需 import。
try:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from etf_data import ETF_UNIVERSE as _ETF_UNIVERSE   # noqa: E402
except Exception:  # noqa: BLE001
    _ETF_UNIVERSE = []

ENGINE_VER = "etf-v1.0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADVISORY_CACHE = os.path.join(ROOT, "etf_advisory_cache.json")  # B 栏已接入数据缓存（离线读，Sina 源）
JISILU_CACHE = os.path.join(ROOT, "jisilu_etf_cache.json")      # B 栏已接入数据缓存（离线读，集思录源：溢价率/规模/费率/净申赎）
TRACKERR_CACHE = os.path.join(ROOT, "etf_track_error_cache.json")  # 第7维「跟踪误差」缓存（etf_track_error.py 经进哥东财 cookie 打通）
REFRESH_STATE = os.path.join(ROOT, "etf_refresh_state.json")    # 刷新状态（冷却/两源结果），供调度器 maybe_refresh 与 etf_refresh.py 共享
MIN_BARS = P.MIN_BARS          # 复用 predictor 的 130 根下限（与共振/每日预测/量化一致）
BT_DATES = quant.BT_DATES      # 回测/学习最多使用的历史交易日数（控成本）
TOP_N_SELECT = 30              # 多因子选股默认出清单只数

# ---- ETF 代码前缀（沪市 51/58/56，深市 15/16/159）---------------------------
# 注意：先匹配更长前缀（如 "159" 同时也是 "15" 前缀），用 startswith 集合判定即可，
# 与 A 股前缀（60/00/30/68/92/43/83/87）不重叠，不会误抓 A 股。
ETF_CODE_PREFIXES = ["51", "58", "56", "15", "16", "159"]

# ---- ETF 专属投顾维度（诚实声明，绝不编造、绝不参学）-------------------------
ETF_ADVISORY_DIMS = [
    {"key": "fund_flow",       "dim": "份额净申赎/资金流",
     "note": "需 akshare/联网取 ETF 份额变化与资金净流入（申赎净额、规模净流入）"},
    {"key": "track_index",     "dim": "跟踪指数与成分",
     "note": "需 akshare/联网取跟踪标的指数与成分股权重"},
    {"key": "premium",         "dim": "溢价率/折溢价",
     "note": "需 akshare/联网取 IOPV 与二级市场价格溢价率"},
    {"key": "scale",           "dim": "规模",
     "note": "需 akshare/联网取最新基金规模(亿元)"},
    {"key": "fee",             "dim": "费率(管理费+托管费)",
     "note": "需 akshare/联网取管理费与托管费年化合计"},
    {"key": "tracking_error",  "dim": "跟踪误差",
     "note": "需 akshare/联网取年化跟踪误差"},
]

DDL = """
CREATE TABLE IF NOT EXISTS etf_run (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date    TEXT NOT NULL,
  state_json  TEXT,
  note        TEXT,
  engine_ver  TEXT,
  created_at  TEXT
);
CREATE TABLE IF NOT EXISTS etf_item (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date    TEXT NOT NULL,
  symbol      TEXT,
  name        TEXT,
  rank        INTEGER,
  score       REAL,
  detail_json TEXT
);
"""

_LOCK = threading.Lock()
_STATUS: dict = {"stage": "", "detail": "", "at": ""}
_PANEL_CACHE: dict = {"key": None, "data": None}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _f(x):
    try:
        return float(x)
    except Exception:  # noqa: BLE001
        return None


def _is_etf(code: str) -> bool:
    """ETF 代码判定：6 位数字且以任一 ETF 前缀开头（覆盖沪市 51/58/56、深市 15/16/159）。"""
    c = str(code or "")
    return (len(c) == 6 and c.isdigit()
            and any(c.startswith(p) for p in ETF_CODE_PREFIXES))


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


# ================================================================ K 线底座
def _read_names(con) -> dict:
    """code → name；con 为 None 时返回空字典（降级用）。"""
    if con is None:
        return {}
    try:
        rows = con.execute("SELECT code, name FROM stock_names").fetchall()
        return {r["code"]: r["name"] for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def etf_universe():
    """扫描本地 K 线缓存 → 返回 (bars_map, updated)。

    bars_map = {etf_code: [(date,o,h,l,c,v), ...]}（升序），仅 ETF 代码且 >= MIN_BARS。
    与 quant._universe_bars 同构（date, open, high, low, close, vol 顺序），但这里额外保留 open。
    当前 predict_kcache.json 有 0 只 ETF 代码 → bars_map 为空（诚实空宇宙）。
    """
    kc = P.load_kcache()
    bars_in = (kc.get("bars") or {})
    updated = kc.get("updated")
    out: dict = {}
    for code, v in bars_in.items():
        if not _is_etf(code):
            continue
        rows = []
        for b in v:
            try:
                rows.append((str(b.get("d"))[:10], _f(b.get("o")),
                             _f(b.get("h")), _f(b.get("l")), _f(b.get("c")), _f(b.get("v"))))
            except Exception:  # noqa: BLE001
                continue
        rows = [r for r in rows if r[0] and r[4]]
        if len(rows) >= MIN_BARS:
            rows.sort(key=lambda x: x[0])
            out[code] = rows
    return out, updated


# ================================================================ 因子面板（复用 quant 的因子计算）
def _etf_panel(codes, bars_map, max_dates=BT_DATES):
    """构建 ETF 截面因子面板（一次性、较重，结果缓存）。

    与 quant._build_panel 同构，但**带 etf 专属缓存 key**（避免与 quant 的 A 股面板缓存串档），
    且**不做 30 只截面下限过滤**——ETF 宇宙天然远小于 A 股，用全部有 ETF 信号的日期，
    让 pooled IC 与多因子打分在 ETF 一旦接入即完全点亮。因子数学全部来自 quant._compute_factor_series。
    """
    series = {}
    by_code = {}
    by_date = {}
    for code in codes:
        rows = bars_map[code]
        fs = quant._compute_factor_series(rows)   # 复用 quant 的 7 因子计算（不重抄）
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
    # ETF 宇宙小：保留全部有 >=1 只 ETF 的信号日（保证候选能在末日被打分）
    dates = [d for d in all_dates if len(by_date[d]) >= 1]
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


def _etf_panel_cached(codes, bars_map, updated):
    """带缓存的面板构建（按 kcache updated + 宇宙规模 判缓存失效；key 带 'etf' 前缀防串档）。"""
    key = ("etf", updated, len(codes))
    if _PANEL_CACHE.get("key") == key and _PANEL_CACHE.get("data"):
        return _PANEL_CACHE["data"]
    data = _etf_panel(codes, bars_map, BT_DATES)
    _PANEL_CACHE["key"] = key
    _PANEL_CACHE["data"] = data
    return data


# ================================================================ A 逻辑：ETF 多因子候选
def etf_factor_candidates(con=None) -> dict:
    """A 逻辑：把 ETF 当作量化多因子候选，复用 quant 的 7 技术因子 + metalearn 权重打分。

    - 候选池来自 etf_universe()（扫描 kcache 的 ETF 代码）。
    - 因子计算 = quant._compute_factor_series（纯本地 K 线离线算）。
    - 权重 = metalearn.weights_from_ic（唯一真源，IC 收缩→只取正→归一→先验混合→下限→精度收口）。
    - 样本外验证 = quant._walkforward_oos（mirrors pillars / quant 的 walk-forward 同构）。
    - 诚实三态徽章：等权运行中 / 收缩生效中 / 样本外验证通过。
    - 当前候选池为空 → 返回 [] 候选 + 提示「ETF 日 K 待接入，候选池为空」；打分逻辑仍完全接好线。
    """
    bars_map, updated = etf_universe()
    codes = list(bars_map.keys())
    if not codes:
        # 诚实空宇宙：打分管线已经接好，只是没有输入数据
        return {
            "ok": True,
            "universe_empty": True,
            "n_universe": 0,
            "data_date": None,
            "weights": {f: round(DEFAULT_W[f], 4) for f in TECH_FACTORS},
            "weight_stage": "等权运行中（ETF 候选池为空）",
            "weights_effective": False,
            "oos": {"n": 0, "win_rate": None, "mean_diff": None, "passed": False},
            "candidates": [],
            "note": ("ETF 日 K 待接入，候选池为空（当前 predict_kcache.json 无 ETF 代码）。"
                     "一旦联网补抓 ETF 日 K 线，候选池与多因子打分将自动点亮，无需改代码。"),
        }
    panel = _etf_panel_cached(codes, bars_map, updated)
    # 各因子在 T+5 上的截面 IC（pooled pearson）与样本数，不足 → None（复用 quant 的 IC 计算）
    ic5, n5 = quant._ic_by_factor(panel, 5)
    # 权重唯一真源 = metalearn.weights_from_ic（禁止在 etf.py 里重抄 IC/收缩/先验公式）
    w, shrunk, learned, has_shrunk, wnote = weights_from_ic(
        ic5, n5, list(TECH_FACTORS), DEFAULT_W)
    # 样本外验证（walk-forward 同构，复用 quant 的实现）
    oos = quant._walkforward_oos(panel, TOP_N_SELECT, DEFAULT_W)
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
    oos_failed = has_shrunk and (oos["n"] or 0) >= quant.OOS_MIN and not oos["passed"]
    if oos_failed:
        weight_stage = "等权运行中（未通过样本外验证 → 本期等权）"
        use_w = dict(DEFAULT_W)

    names = _read_names(con) if con is not None else {}
    last = panel["dates"][-1] if panel["dates"] else None
    scores = quant._composite_at(panel, last, use_w) if last else {}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:TOP_N_SELECT]
    candidates = []
    for rank, (code, sc) in enumerate(ranked, 1):
        fs = panel["series"][code]
        fac_scores = {}
        # 取最近一个有效索引（末根 bar 若停牌/缺口则向前找）
        last_i = len(bars_map[code]) - 1
        while last_i >= 0 and fs["idx_date"][last_i] is None:
            last_i -= 1
        for f in TECH_FACTORS:
            ov = fs[f]["oriented"][last_i] if last_i >= 0 else None
            fac_scores[f] = round(ov, 4) if ov is not None else None
        candidates.append({
            "rank": rank,
            "symbol": code,
            "name": names.get(code, ""),
            "score": round(sc, 4),
            "factor_scores": fac_scores,
        })
    return {
        "ok": True,
        "universe_empty": False,
        "n_universe": len(codes),
        "data_date": last,
        "weights": {f: round(use_w[f], 4) for f in TECH_FACTORS},
        "weight_stage": weight_stage,
        "weights_effective": (weight_stage.startswith("样本外验证通过")
                              or weight_stage.startswith("收缩生效中")),
        "oos": oos,
        "candidates": candidates,
        "note": ("ETF 作为量化多因子候选：复用 quant 的 7 技术因子（MA_POS/MACD/KDJ/BOLL/VOL_ABS/MOM/VOLA）"
                 "纯本地 K 线离线算，权重走 metalearn.weights_from_ic 单一真源，绝不在 etf.py 里再抄权重公式。"
                 "当前候选池 %d 只；样本外验证用 walk-forward（每期用前期 IC 学权重→打分→下期收益），非真实选股复盘。"
                 % len(codes)),
    }


# ================================================================ B 逻辑：ETF 专属投顾维度
def _load_advisory_cache() -> dict | None:
    """离线读 B 栏已接入数据缓存（etf_advisory_data.py 产出，请求路径不依赖 akshare/联网）。"""
    try:
        with open(ADVISORY_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _load_jisilu_cache() -> dict | None:
    """离线读集思录 ETF 缓存（etf_jisilu.py 产出：溢价率/规模/费率/真实份额净申赎）。"""
    try:
        return json.load(open(JISILU_CACHE, encoding="utf-8")).get("etfs") or {}
    except Exception:  # noqa: BLE001
        return None


def _load_track_error_cache() -> dict | None:
    """离线读第7维「跟踪误差」缓存（etf_track_error.py 产出：东财 F10 年化跟踪误差）。

    返回完整 cache dict（含 as_of / n / track_error），无文件或异常时返回 None。
    该维默认不接入：东财 F10 在沙箱 IP 被风控，需在进哥部署机 + 东财 cookie 跑脚本生成缓存。
    """
    try:
        return json.load(open(TRACKERR_CACHE, encoding="utf-8")) or None
    except Exception:  # noqa: BLE001
        return None


def etf_advisory() -> dict:
    """B 逻辑：ETF 专属投顾 6 维 + 实时行情快照 + 集思录投顾指标。

    离线读两份缓存（请求路径不依赖 akshare/联网、不依赖集思录 cookie）：
      - etf_advisory_cache.json（Sina 源）：资金流/行情、跟踪指数（名称解析）。
      - jisilu_etf_cache.json（集思录源，etf_jisilu.py 经进哥提供的登录态 cookie 打通）：
        溢价率(nav_discount_rt)、规模(份额×净值估算)、费率(管理费+托管费)、真实份额净申赎。
      - 跟踪误差：集思录 etf_list 接口未提供 → 诚实标「待接入」，绝不编造数值、绝不参学。
    返回 {dims:[6], quotes:{...}, quotes_meta:{...}, jisilu:{code:{...}}}。
    """
    cache = _load_advisory_cache()
    quotes_all = (cache or {}).get("quotes") or {}
    track_all = (cache or {}).get("track_index") or {}
    as_of = (cache or {}).get("as_of")

    jl_all = _load_jisilu_cache() or {}
    jl_as_of = None
    try:
        jl_as_of = json.load(open(JISILU_CACHE, encoding="utf-8")).get("as_of")
    except Exception:  # noqa: BLE001
        pass

    # 2026-10-17 修复（A2·架构红线）：B 栏宇宙**不再**依赖 A 栏的 kcache（etf_universe），
    # 否则一旦 kcache 无 ETF 代码，接好的 1626+1437 条真数据会被全屏蔽成假空「待接入」。
    # 改为：以「ETF 目标宇宙」∩ 本模块已接入的缓存键为宇宙，与 A 栏 kcache 彻底解耦。
    # 极端兜底层：若目标宇宙为空（etf_data 未导入），直接用缓存键，保证 B 栏永不因门控而假空。
    cache_codes = set(quotes_all.keys()) | set(jl_all.keys())
    _base = _ETF_UNIVERSE if _ETF_UNIVERSE else cache_codes
    our = [c for c in _base if c in cache_codes]
    if not our:  # 兜底：直接用缓存键，保证 B 栏永不因门控而假空
        our = list(cache_codes)
    quotes = {c: quotes_all[c] for c in our if c in quotes_all}
    track = {c: track_all.get(c) for c in our if c in track_all}
    jl_ours = {c: jl_all[c] for c in our if c in jl_all}
    # 第7维「跟踪误差」：东财 F10 缓存（若进哥已跑 etf_track_error.py 生成）
    te_cache = _load_track_error_cache()
    te_all = (te_cache or {}).get("track_error") or {}
    te_as_of = (te_cache or {}).get("as_of")
    if te_all:
        for c in jl_ours:
            if c in te_all:
                jl_ours[c]["track_error"] = (te_all[c] or {}).get("track_err")
    n = len(quotes)
    nj = len(jl_ours)

    flow_status = "已接入(代理)" if n else "待接入"
    idx_status = "已接入(指数名)" if track else "待接入"
    jl_on = nj > 0

    # 资金流：升级为「Sina 成交额代理 + 集思录真实份额净申赎」
    flow_note = ("已接入：Sina 实时成交额作资金活跃度代理（%d 只）；集思录提供真实份额净申赎（%d 只，as_of %s）。"
                 % (n, nj, jl_as_of)) if (n or jl_on) else "需 akshare/联网取 ETF 份额变化与资金净流入。"

    dims = [
        {"key": "fund_flow", "dim": "份额净申赎/资金流",
         "status": flow_status,
         "value": {"n": n, "label": "当日成交额(元)", "net_subscribe_n": nj} if n else None,
         "note": flow_note},
        {"key": "track_index", "dim": "跟踪指数与成分",
         "status": idx_status,
         "value": {"n": len(track), "sample": dict(list(track.items())[:12])} if track else None,
         "note": ("已接入：由 ETF 名称解析跟踪指数（如 沪深300/创业板/科创50，共 %d 只）；集思录补充精确跟踪指数名 %d 只。"
                  % (len(track), sum(1 for c in jl_ours if jl_ours[c].get("index_nm")))) if track else
                 "需 akshare/联网取跟踪标的指数与成分股权重。"},
        {"key": "premium", "dim": "溢价率/折溢价",
         "status": "已接入" if jl_on else "待接入",
         "value": {"n": nj, "label": "净值折溢价率(%)"} if jl_on else None,
         "note": ("已接入：集思录 nav_discount_rt（净值折溢价率，%d 只，as_of %s）。" % (nj, jl_as_of)) if jl_on
                 else "需集思录/东财 IOPV 与二级市场价格溢价率（待接入）。"},
        {"key": "scale", "dim": "规模",
         "status": "已接入(估算)" if jl_on else "待接入",
         "value": {"n": nj, "label": "估算规模=份额×净值(亿元)"} if jl_on else None,
         "note": ("已接入(估算)：集思录总份额×单位净值（%d 只，as_of %s，单位亿元）；精确规模需东财 F10。"
                  % (nj, jl_as_of)) if jl_on else "需东财基金规模(亿元)（待接入）。"},
        {"key": "fee", "dim": "费率(管理费+托管费)",
         "status": "已接入" if jl_on else "待接入",
         "value": {"n": nj, "label": "年化管理费+托管费(%)"} if jl_on else None,
         "note": ("已接入：集思录 fee（管理费+托管费年化合计，%d 只，as_of %s）。" % (nj, jl_as_of)) if jl_on
                 else "需 akshare 管理费+托管费合计（待接入）。"},
        {"key": "tracking_error", "dim": "跟踪误差",
         "status": ("已接入" if te_all else "待接入"),
         "value": ({"n": sum(1 for c in jl_ours if c in te_all),
                    "approx_n": sum(1 for c in jl_ours if (te_all.get(c) or {}).get("approx")),
                    "label": "年化跟踪误差(%)"}) if te_all else None,
         "note": ("已接入：东财 F10 年化跟踪误差（%d 只，as_of %s；其中 %d 只为同指数联接基金近似，界面标「近似值·联接基金」）。"
                   % (sum(1 for c in jl_ours if c in te_all), te_as_of,
                      sum(1 for c in jl_ours if (te_all.get(c) or {}).get("approx")))) if te_all else
                  "公开源无此字段（akshare 无函数、集思录 etf_list 无）；东财 F10 需在部署机+东财 cookie 跑 etf_track_error.py 打通（沙箱 IP 被风控）。"},
    ]
    return {
        "dims": dims,
        "quotes": quotes,
        "quotes_meta": {"source": (cache or {}).get("source"), "as_of": as_of, "n": n},
        "jisilu": jl_ours,
        "jisilu_meta": {"as_of": jl_as_of, "n": nj, "source": "jisilu.cn/data/etf/etf_list"},
    }


# ================================================================ 只读 payload / status
def payload(con: sqlite3.Connection) -> dict:
    """左 A（量化多因子 ETF 候选）/ 右 B（ETF 专属投顾 6 维）分离 payload（关键交付：两栏是两个独立对象）。"""
    ensure_tables(con)
    # 左 A：实时算（复用面板缓存），候选池为空时诚实返回 [] + 提示
    try:
        left_data = etf_factor_candidates(con=con)
    except Exception as e:  # noqa: BLE001
        left_data = {"ok": False, "error": str(e)[:200], "universe_empty": True,
                     "n_universe": 0, "candidates": [], "weights": {f: round(DEFAULT_W[f], 4) for f in TECH_FACTORS},
                     "weight_stage": "等权运行中（计算异常）", "weights_effective": False}
    # 右 B：6 维投顾（资金流/行情/跟踪指数已接入 Sina；溢价率/规模/费率已接入集思录；跟踪误差诚实待接入）
    right_raw = etf_advisory()
    left = {
        "type": "A",
        "title": "量化多因子中的 ETF 候选",
        "desc": "把 ETF 当作量化多因子候选：复用 quant 的 7 技术因子（纯本地 K 线离线算）+ metalearn 权重；"
                "候选池为空时诚实返回空。",
        "ok": left_data.get("ok"),
        "universe_empty": left_data.get("universe_empty", False),
        "n_universe": left_data.get("n_universe", 0),
        "data_date": left_data.get("data_date"),
        "weights": left_data.get("weights"),
        "weight_stage": left_data.get("weight_stage"),
        "weights_effective": left_data.get("weights_effective", False),
        "candidates": left_data.get("candidates", []),
        "note": left_data.get("note", ""),
    }
    right = {
        "type": "B",
        "title": "ETF 专属投顾维度",
        "desc": "ETF 专属的 6 个投顾维度（份额净申赎/资金流、跟踪指数与成分、溢价率/折溢价、规模、费率、跟踪误差）；"
                "资金流/行情与跟踪指数已接入 Sina，溢价率/规模/费率已接入集思录（进哥提供的登录态 cookie 打通），"
                "跟踪误差需东财 F10（本沙箱待接）诚实待接入，绝不编造。",
        "dims": right_raw["dims"],
        "quotes": right_raw["quotes"],
        "quotes_meta": right_raw["quotes_meta"],
        "jisilu": right_raw["jisilu"],
        "jisilu_meta": right_raw["jisilu_meta"],
    }
    return {
        "ok": True,
        "engine_ver": ENGINE_VER,
        "generated_at": _now(),
        "left": left,
        "right": right,
        "note": ("左栏 A = 把 ETF 当作量化多因子候选：复用 quant 的 7 技术因子 + metalearn 权重，"
                 "候选池为空时诚实返回空 + 提示。右栏 B = ETF 专属投顾 6 维"
                 "（份额净申赎/资金流、跟踪指数与成分、溢价率/折溢价、规模、费率、跟踪误差），"
                 "全部「待接入」（需 akshare/联网），绝不编造。A/B 左右对称、独立成栏，绝不混叠。"),
    }


def status_payload(con: sqlite3.Connection) -> dict:
    ensure_tables(con)
    return {"engine_ver": ENGINE_VER, "stage": _STATUS.get("stage"),
            "detail": _STATUS.get("detail"), "at": _STATUS.get("at"),
            "today": _today(),
            "runs": [{"run_date": r["run_date"], "items": r["n"]} for r in con.execute(
                "SELECT run_date, COUNT(*) n FROM etf_item GROUP BY run_date ORDER BY run_date DESC LIMIT 8")]}


# ================================================================ 批量落库入口（POST /api/etf/run）
def run(con: sqlite3.Connection, td: str | None = None, force: bool = False) -> dict:
    """落库入口（POST /api/etf/run）：重算 ETF 多因子候选并落库（mirrors quant.run 的 con/db 获取模式）。

    失败不崩主流程；候选池为空时也记录一次空跑（状态诚实可见）。
    """
    with _LOCK:
        t0 = time.time()
        try:
            _STATUS.update(stage="载入ETF K线底座", detail="", at=_now())
            bars_map, _ = etf_universe()
            if not bars_map:
                td = td or _today()
                ensure_tables(con)
                con.execute("DELETE FROM etf_run WHERE run_date=?", (td,))
                con.execute("INSERT INTO etf_run(run_date,state_json,note,engine_ver,created_at) "
                            "VALUES(?,?,?,?,?)",
                            (td, json.dumps({}, ensure_ascii=False),
                             "ETF 日 K 待接入，候选池为空", ENGINE_VER, _now()))
                con.commit()
                return {"ok": True, "run_date": td, "empty": True,
                        "elapsed": round(time.time() - t0, 1),
                        "note": "ETF 候选池为空，未产生候选；一旦 ETF 日 K 接入即自动点亮。"}
            td = td or P._trade_date_of(bars_map)
            if not td:
                td = _today()
            if not force:
                row = con.execute("SELECT run_date FROM etf_run WHERE run_date=?", (td,)).fetchone()
                if row:
                    return {"ok": True, "run_date": td, "cached": True}
            _STATUS.update(stage="计算ETF多因子候选", detail=td, at=_now())
            left = etf_factor_candidates(con=con)
            ensure_tables(con)
            con.execute("DELETE FROM etf_run WHERE run_date=?", (td,))
            con.execute("DELETE FROM etf_item WHERE run_date=?", (td,))
            con.execute("INSERT INTO etf_run(run_date,state_json,note,engine_ver,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (td, json.dumps({
                            "weights": left.get("weights"),
                            "weight_stage": left.get("weight_stage"),
                            "n_universe": left.get("n_universe"),
                            "universe_empty": left.get("universe_empty"),
                        }, ensure_ascii=False), left.get("note", ""), ENGINE_VER, _now()))
            for it in left.get("candidates", []):
                con.execute("INSERT INTO etf_item(run_date,symbol,name,rank,score,detail_json) "
                            "VALUES(?,?,?,?,?,?)",
                            (td, it["symbol"], it.get("name") or it["symbol"], it["rank"], it["score"],
                             json.dumps({"factor_scores": it.get("factor_scores", {})}, ensure_ascii=False)))
            con.commit()
            _STATUS.update(stage="完成", detail="", at=_now())
            return {"ok": True, "run_date": td, "engine_ver": ENGINE_VER,
                    "elapsed": round(time.time() - t0, 1), **left}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}


# ================================================================ 缓存自动刷新（融入 server 调度器 + 可手动/外部计划触发）
def _load_refresh_state() -> dict:
    try:
        with open(REFRESH_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_refresh_state(st: dict) -> None:
    try:
        tmp = REFRESH_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, REFRESH_STATE)   # 原子替换，避免读一半的半截文件
    except Exception:  # noqa: BLE001
        pass


def refresh_caches(force: bool = False) -> dict:
    """刷新 B 栏两个数据缓存（盘后跑一次即可）：
      - Sina ETF 实时行情/跟踪指数：etf_advisory_data.fetch()（akshare，需 venv 运行）
      - 集思录投顾指标（溢价率/规模/费率/真实份额净申赎）：etf_jisilu.fetch()（需进哥 jisilu cookie）
    两源各自 try/except 隔离，单源失败不影响另一源；结果写入 etf_refresh_state.json 供调度器冷却与排障。
    请求路径不依赖 akshare 常驻——akshare 仅在 Sina 源真正刷新时才在函数内动态 import。
    """
    res = {"date": _today(), "forced": bool(force), "sina": None, "jisilu": None}
    # 源1：Sina ETF 行情（akshare，venv 可用）
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import etf_advisory_data as ead  # 动态 import：仅在刷 Sina 源时引入 akshare
        out = ead.fetch(force=True)
        res["sina"] = {"ok": True, "n": out.get("n_total"), "as_of": out.get("as_of")}
        logging.info("ETF[Sina] 缓存刷新成功：%d 只 · as_of %s", out.get("n_total"), out.get("as_of"))
    except Exception as e:  # noqa: BLE001
        res["sina"] = {"ok": False, "error": str(e)[:300]}
        logging.exception("ETF[Sina] 缓存刷新失败")
    # 源2：集思录投顾指标（需 cookie）
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import etf_jisilu
        cache = etf_jisilu.fetch()
        n = cache.get("n_total") if cache else 0
        res["jisilu"] = {"ok": bool(n), "n": n, "as_of": (cache or {}).get("as_of")}
        if not n:
            res["jisilu"]["warn"] = "集思录返回 0 只：cookie 可能已失效，请进哥更新 jisilu_cookie.txt"
            logging.warning("ETF[集思录] 返回 0 只，cookie 可能失效")
    except Exception as e:  # noqa: BLE001
        res["jisilu"] = {"ok": False, "error": str(e)[:300]}
        logging.exception("ETF[集思录] 缓存刷新失败")
    full_ok = bool((res.get("sina") or {}).get("ok")) and bool((res.get("jisilu") or {}).get("ok"))
    res["full_ok"] = full_ok
    if force or full_ok:
        res["last_date"] = _today()   # 仅成功（或强制）才记冷却日期，避免天天重试失败
    _save_refresh_state(res)
    logging.info("ETF 缓存刷新完成：%s", json.dumps(res, ensure_ascii=False)[:400])
    return res


# ================================================================ A 栏日 K 调度刷新（A1 修复：接进 server 调度器）
_KLINE_STATE = os.path.join(ROOT, "etf_kline_state.json")


def _load_kline_state() -> dict:
    try:
        with open(_KLINE_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_kline_state(st: dict) -> None:
    try:
        tmp = _KLINE_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, _KLINE_STATE)
    except Exception:  # noqa: BLE001
        pass


def maybe_refresh_klines() -> None:
    """A1 修复：调度器钩子，每日盘后刷新一次 ETF 日 K 进 predict_kcache.json（A 栏底座）。

    与 maybe_refresh（B 缓存）对称：交易日 + 16:00 后 + 当日冷却才跑；非交易日/盘前跳过；
    失败隔离，不阻塞主进程。干净部署后也能自动点亮 A 栏候选池，不再依赖手动跑 etf_data.py。
    """
    try:
        now = datetime.now()
        if (now.hour, now.minute) < (16, 0):
            return
        try:
            import auction
            if auction.is_trading_day() is not True:
                return
        except Exception:  # noqa: BLE001
            pass
        st = _load_kline_state()
        if st.get("last_date") == _today() and st.get("ok"):
            return
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import etf_data
        r = etf_data.fetch_etf_klines(force=False)
        _save_kline_state({"last_date": _today(), "ok": bool(r.get("ok")),
                           "n": r.get("n"), "updated": r.get("updated")})
        logging.info("ETF 日K 调度刷新完成：%s", json.dumps(r, ensure_ascii=False)[:200])
    except Exception:  # noqa: BLE001
        logging.exception("ETF maybe_refresh_klines 异常（已忽略，下轮再试）")


# ================================================================ ETF 融合信号（回灌今日决策四卡）
# 板块别名桥：sector_chain 板块名 → ETF track_index 关键词（化学反应靠这层映射）。
# 覆盖主流行业；未列出的板块 ETF 信号自然不挂（诚实无覆盖，绝不编造）。
SECTOR_ALIAS = {
    "半导体": ["半导体", "芯片"], "芯片": ["芯片", "半导体"],
    "新能源车": ["新能源车", "汽车"], "汽车": ["汽车", "新能源车"],
    "医药": ["医药", "创新药", "医疗", "生物"], "创新药": ["创新药", "医药", "生物"],
    "军工": ["军工", "国防"], "券商": ["证券", "券商"], "证券": ["证券", "券商"],
    "金融": ["金融", "证券", "银行", "保险"], "消费": ["消费"], "白酒": ["白酒", "消费"],
    "科技": ["科技", "科创", "创业"], "科创": ["科创", "科技"], "创业板": ["创业板"],
    "红利": ["红利", "股息"], "煤炭": ["煤炭"], "有色": ["有色", "金属"],
    "化工": ["化工", "材料"], "光伏": ["光伏", "太阳能"], "储能": ["储能"],
    "AI": ["人工智能", "AI", "智能"], "人工智能": ["人工智能", "AI"], "机器人": ["机器人"],
    "通信": ["通信", "5G"], "5G": ["5G", "通信"], "传媒": ["传媒", "游戏"],
    "计算机": ["计算机", "软件"], "养殖": ["养殖", "猪肉", "农业"], "农业": ["农业", "养殖"],
    "地产": ["地产", "房地产"], "钢铁": ["钢铁"], "电力": ["电力", "能源"],
    "能源": ["能源", "电力"], "黄金": ["黄金"], "中概": ["中概", "互联"], "港股": ["恒生", "港股"],
}
_CROSS_BORDER = ["513050", "513100", "513500", "159920", "513180", "159941", "161725", "513090"]
_BROAD_BASED = ["510300", "510330", "510310", "159919", "510050", "510100",
                "510500", "510510", "159922", "588000", "588080", "159915"]
_LEVEL_RANK = {"强": 3, "中": 2, "弱": 1, "观测": 0}


def etf_decision_overlay() -> dict:
    """把 ETF（Sina 行情 + 集思录净申赎/折溢价）聚合成可回灌「今日决策」的三类信号。

    纯本地读缓存（etf_advisory_cache.json / jisilu_etf_cache.json），无网络、无 DB、无 kcache 依赖。
    返回：sector_flow（板块资金流）/ cross_border（跨境风险传导）/ broad_based（宽基仓位 regime）/
          confirm_map（track_index→sector_chain 板块的资金确认，供方向卡匹配）。
    诚实：无匹配板块则不挂标签，绝不编造。
    """
    cache = _load_advisory_cache() or {}
    quotes_all = cache.get("quotes") or {}
    tk_all = cache.get("track_index") or {}
    jl_all = _load_jisilu_cache() or {}
    as_of = cache.get("as_of")

    # 1) 按 track_index 聚合板块资金流
    grp: dict = {}
    for c, q in quotes_all.items():
        ti = tk_all.get(c) or ""
        if not ti:
            continue
        g = grp.setdefault(ti, {"pcts": [], "incr": [], "codes": []})
        g["codes"].append(c)
        if q.get("pct") is not None:
            g["pcts"].append(q["pct"])
        jv = jl_all.get(c) or {}
        inc = jv.get("amount_incr")
        if isinstance(inc, (int, float)) and inc != 0:
            g["incr"].append(inc)

    sector_flow = []
    for ti, g in grp.items():
        avg_pct = round(sum(g["pcts"]) / len(g["pcts"]), 2) if g["pcts"] else None
        net = round(sum(g["incr"]) / 10000.0, 2) if g["incr"] else 0.0  # amount_incr 单位亿份→亿
        if g["incr"]:
            pos = sum(1 for x in g["incr"] if x > 0)
            neg = sum(1 for x in g["incr"] if x < 0)
            level = "强" if (pos > neg and (avg_pct is None or avg_pct >= -1)) else ("中" if pos >= neg else "弱")
        else:
            level = "观测"
        sector_flow.append({"track": ti, "etf_count": len(g["codes"]),
                            "avg_pct": avg_pct, "net_subscribe_yi": net, "level": level})

    # 2) 跨境 ETF 风险传导（海外风险向 A 股传导的领先信号；风险信号拥有最高否决权）
    cross = {"risk": False, "items": []}
    for c in _CROSS_BORDER:
        q = quotes_all.get(c)
        jv = jl_all.get(c) or {}
        if not q:
            continue
        pct = q.get("pct")
        prem = jv.get("premium_rt")
        name = q.get("name") or (jl_all.get(c) or {}).get("name") or c
        item = {"code": c, "name": name, "pct": pct, "premium_rt": prem, "flag": ""}
        if pct is not None and pct <= -2.0:
            item["flag"] = "海外下跌传导"
            cross["risk"] = True
        if isinstance(prem, (int, float)) and prem >= 5.0:
            item["flag"] = (item["flag"] + "；" if item["flag"] else "") + "高溢价过热"
            cross["risk"] = True
        cross["items"].append(item)

    # 3) 宽基 ETF 仓位/牛熊 regime（长线资金托底信号）
    bb_incr = 0.0
    bb_pcts = []
    bb_codes = []
    for c in _BROAD_BASED:
        jv = jl_all.get(c) or {}
        inc = jv.get("amount_incr")
        if isinstance(inc, (int, float)):
            bb_incr += inc
        q = quotes_all.get(c)
        if q and q.get("pct") is not None:
            bb_pcts.append(q["pct"])
        if q:
            bb_codes.append(c)
    bb_avg = round(sum(bb_pcts) / len(bb_pcts), 2) if bb_pcts else None
    bb_net = round(bb_incr / 10000.0, 2)
    regime = "risk_on" if bb_net > 50 else ("risk_off" if bb_net < -20 else "neutral")
    broad = {"regime": regime, "net_subscribe_yi": bb_net, "avg_pct": bb_avg, "codes": bb_codes[:6]}

    # 4) confirm_map：板块别名桥 → sector_chain 板块资金确认（化学反应落点）
    confirm_map = {}
    for sec, kws in SECTOR_ALIAS.items():
        incrs = []
        pcts = []
        codes = []
        for ti, g in grp.items():
            if ti in kws or any(k and (k in ti or ti in k) for k in kws):
                incrs += g["incr"]
                pcts += g["pcts"]
                codes += g["codes"]
        if not codes:
            continue
        avg_pct = round(sum(pcts) / len(pcts), 2) if pcts else None
        net = round(sum(incrs) / 10000.0, 2) if incrs else 0.0
        pos = sum(1 for x in incrs if x > 0)
        neg = sum(1 for x in incrs if x < 0)
        level = "强" if (incrs and pos > neg and (avg_pct is None or avg_pct >= -1)) else (
            "中" if (incrs and pos >= neg) else ("弱" if incrs else "观测"))
        confirm_map[sec] = {"level": level, "avg_pct": avg_pct, "net_subscribe_yi": net,
                            "etf_count": len(codes), "codes": codes[:5]}

    return {"ok": True, "as_of": as_of, "sector_flow": sector_flow,
            "cross_border": cross, "broad_based": broad, "confirm_map": confirm_map}


def maybe_refresh() -> None:
    """调度器钩子（_sched_tasks 中注册）：每日盘后只刷新一次，随查看器自动跑。
    非交易日 / 盘前(16:00 前) / 今日已成功刷新 均快速跳过，不联网、不阻塞主进程。
    """
    try:
        now = datetime.now()
        if (now.hour, now.minute) < (16, 0):       # A 股收盘后（16:00）才刷，避免抓盘中半截/休市空数据
            return
        try:                                        # 仅交易日刷新（复用项目既有 is_trading_day）
            import auction
            if auction.is_trading_day() is not True:
                return
        except Exception:  # noqa: BLE001
            pass  # 交易日判断失败不阻塞，仍尝试刷新
        st = _load_refresh_state()
        if st.get("last_date") == _today() and st.get("full_ok"):
            return  # 今日已完整刷新过 → 冷却跳过
        refresh_caches(force=False)
    except Exception:  # noqa: BLE001
        logging.exception("ETF maybe_refresh 异常（已忽略，下轮再试）")
