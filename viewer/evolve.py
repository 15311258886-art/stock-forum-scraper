# -*- coding: utf-8 -*-
"""自我进化引擎（evolve-v1.0，2026-10-15 进哥需求）。

定位：这不是"再加一个看板"，而是把前面三个支柱的沉淀**消化成自己的东西**的工序。
进哥原话：「吸取他们、了解他们、应用他们，最终都要总结成自己的东西，而不是说一味地模仿，
永远模仿是没法超越的」—— 本模块就是那道"总结成自己的"环节。

四件事（全部本地离线算，零网络）：

① **权重自学习** —— 共振分那几个权重，不该由人拍脑袋定。
   对每个维度算信息系数 IC（该维度取值 与 未来 T+5 超额 的相关系数），
   按 max(0, IC) 归一化得到权重。
   ⇒ **博主维度整体负贡献时，它的权重会被数据自动压到接近 0**（这就是"不模仿"的机制），
     而不是我们嘴上说说"博主要谨慎用"。

② **规则挖掘** —— 从「市场环境 × 博主声量 × 战法命中 × 舆情 × 龙虎榜」的组合空间里，
   挖出样本充足、超额显著的条件组合，做成候选规则；
   用**时间序 70/30 切分**做样本外验证，通过者晋升为「已采纳」并反哺共振评分。
   ⇒ 这些规则不是任何一位博主说过的，是数据里自己长出来的。

③ **衰减与信誉进化** —— 每个信源的「近 20 日超额 vs 全期超额」，
   标出「上升 / 稳定 / 衰减」。谁在退步，系统自己认得出来。

④ **知识内化 + 每日进化日志** —— 把挖掘结果与图谱互证写成"我们自己的方法论条目"
   （含数据证据与来源引用），并与上一次运行对比，记录"今天学到了什么"。

反哺闭环（最关键、也是判断这套东西是不是花架子的唯一标准）：
  学出的权重写入 evo_weight → resonance.build_cards 每期读取最新权重 →
  学完当场重算一遍共振卡（refresh_cards）→ **学习当天就改变产出**。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import predictor as P
import resonance
from metalearn import (MIN_IC_N, IC_SHRINK_K, W_PRIOR_MIX, W_FLOOR,
                      pearson as _pearson, weights_from_ic)

ENGINE_VER = "evolve-v1.0"

# ---- 口径常量（改动须附证据）--------------------------------------------------
SAMPLE_DAYS = 48          # 采样交易日数（需给 T+5 回填留出尾部，故实际取 [-SAMPLE-6:-6]）
MIN_RULE_N = 20           # 入规则库的最小样本
OOS_SPLIT = 0.7           # 时间序切分：前 70% 训练 / 后 30% 样本外
RULE_LIFT_MIN = 1.0       # 候选规则最小超额提升（百分点，相对全样本基线）
# ⚠️ 第一版实测踩的坑：纯 IC 归一化会把四维权重**塌缩成单维**（实测 strategy 拿 100%，
# 其余三维全 0，因为它们的 IC 恰好为负）。多维融合直接退化成"只看战法命中数"。
# 故：① 先验收缩（保留 40% 默认权重，防单期数据过拟合）② 单维度下限 6%（保多源结构）。
# ↑ 上述「收缩K / 先验混合 / 单维下限 / IC最小样本」四类口径常量已统一收口到 metalearn.py
#   （单一真源）；evolve 与 pillars 与量化板都从这里 import，改动须回 metalearn 并附证据。
RULE_ADOPT_OOS = 0.0      # 晋升「已采纳」要求样本外平均超额 > 此值
RECENT_DAYS = 20          # 衰减检测的「近期」窗口（交易日）
TREND_EPS = 1.0           # 近远期超额差 > 此值才判上升/衰减（百分点）
DIMS = ["blogger", "strategy", "senti", "engine"]
# 2026-10-30 P1-b 埋点：本模块的样本 `y` 固定是「信号日次日买入、持有 5 个交易日」的
#   超额（见 `_build_rows` 里 bi=i0+1 → fut=b[bi+5]）。`evo_weight.by_state_json` 里的
#   per-state 权重就是在这个口径上学出来的。**这个数字必须能被下游读到**，否则
#   "用哪个持有期的 IC 学的权重"只存在于注释里，改口径时无法察觉。
FIT_HORIZON = 5
AUX_DIMS = ["lhb"]        # 只做诊断展示（进哥：龙虎榜作印证不作预判），不进共振权重
DIM_LABEL = {"blogger": "博主声量", "strategy": "战法命中", "senti": "论坛舆情",
             "engine": "主引擎交叉", "lhb": "龙虎榜印证"}
DEFAULT_W = {"blogger": 0.34, "strategy": 0.34, "senti": 0.14, "engine": 0.18}

DDL = """
-- ① 学出的权重（每日一版，可回溯）
CREATE TABLE IF NOT EXISTS evo_weight (
  fit_date       TEXT PRIMARY KEY,
  n_rows         INTEGER,
  ic_json        TEXT,
  w_json         TEXT,
  w_default_json TEXT,
  by_state_json  TEXT,
  w_blogger      REAL,
  w_strategy     REAL,
  w_senti        REAL,
  w_engine       REAL,
  note           TEXT,
  created_at     TEXT
);
-- ② 规则库（自己长出来的技术）
CREATE TABLE IF NOT EXISTS evo_rule (
  rid        TEXT PRIMARY KEY,
  cond_json  TEXT,
  cond_text  TEXT,
  n          INTEGER,
  win_rate   REAL,
  avg_ex     REAL,
  baseline   REAL,
  lift       REAL,
  oos_n      INTEGER,
  oos_avg_ex REAL,
  oos_win    REAL,
  status     TEXT,
  updated_at TEXT
);
-- ③ 知识内化（我们自己的方法论条目，非他人原话）
CREATE TABLE IF NOT EXISTS evo_knowledge (
  kid        TEXT PRIMARY KEY,
  kind       TEXT,
  title      TEXT,
  body       TEXT,
  evidence   TEXT,
  sources    TEXT,
  status     TEXT,
  updated_at TEXT
);
-- ④ 每日进化日志
CREATE TABLE IF NOT EXISTS evo_journal (
  log_date          TEXT PRIMARY KEY,
  learned           TEXT,
  weight_json       TEXT,
  weight_delta_json TEXT,
  rules_new         INTEGER,
  rules_dead        INTEGER,
  src_moves         TEXT,
  stats_json        TEXT,
  created_at        TEXT
);
-- ⑤ 信源趋势（上升 / 稳定 / 衰减）
CREATE TABLE IF NOT EXISTS evo_source_trend (
  src_type   TEXT NOT NULL,
  src_key    TEXT NOT NULL,
  ex_all     REAL,
  ex_recent  REAL,
  delta      REAL,
  trend      TEXT,
  n_all      INTEGER,
  n_recent   INTEGER,
  updated_at TEXT,
  PRIMARY KEY (src_type, src_key)
);
"""

_LOCK = threading.Lock()
_STATUS: dict = {"stage": "", "detail": "", "at": ""}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _since(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


# ================================================================ 统计工具（纯标准库）
def _mean(xs) -> float:
    return sum(xs) / float(len(xs)) if xs else 0.0


def _win_rate(xs) -> float:
    return (sum(1 for x in xs if x > 0) / float(len(xs))) if xs else 0.0


# _pearson 已收口到 metalearn.pearson（顶部 `from metalearn import ... pearson as _pearson`），
# 此处不再重复定义，evolve 全文件统一走共享引擎。

def _bucket_blogger(v: float) -> str:
    return "0" if v <= 0 else ("1" if v < 1.5 else "2+")


def _bucket_strat(v: float) -> str:
    return "0" if v <= 0 else ("1-2" if v <= 2 else "3+")


def _bucket_senti(v: float) -> str:
    return "净多" if v > 0 else ("净空" if v < 0 else "中性")


def _bucket_lhb(v: float) -> str:
    return "无" if v == 0 else ("正向" if v > 0 else "负向")


# ================================================================ 特征工程
def _strategy_daily(codes: set) -> dict:
    """历史战法命中回放：{code: {date: 命中数}}（剔除择时类，与共振口径一致）。

    用 predictor.sig_tech_replay 一次算完整序列（与 sig_tech 同量级成本），
    这样战法维度才有**历史**可用于学习 —— 否则只有最新一天、样本为零。
    """
    sub = resonance._load_sig_bars(codes)
    if not sub:
        return {}
    try:
        strat = P._load_strategies()
    except Exception:  # noqa: BLE001
        strat = []
    if not strat:
        return {}
    out: dict = {}
    for code, k in sub.items():
        try:
            seqs = P.sig_tech_replay(k, strat)
        except Exception:  # noqa: BLE001
            continue
        dates = [b.get("d") for b in k]
        cnt: dict = {}
        for key, seq in (seqs or {}).items():
            if key in resonance.TIMING_STRATS:
                continue
            for i, v in enumerate(seq or []):
                if v and i < len(dates):
                    d = dates[i]
                    cnt[d] = cnt.get(d, 0) + 1
        if cnt:
            out[code] = cnt
    return out


def _senti_daily(con: sqlite3.Connection) -> dict:
    """{code: {date: 情绪净量（看多-看空）}}，用于按采样日累加窗口值。"""
    try:
        rows = con.execute(
            "SELECT m.symbol AS sym, substr(p.post_time,1,10) AS d, p.sentiment AS sn "
            "FROM posts p JOIN stock_mentions m ON m.post_id_ref = p.id "
            "WHERE p.post_time >= ? AND m.symbol <> ''", (_since(150),)).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for r in rows:
        d = r["d"]
        if not d:
            continue
        v = 1 if r["sn"] == "看多" else (-1 if r["sn"] == "看空" else 0)
        if not v:
            continue
        out.setdefault(r["sym"], {})
        out[r["sym"]][d] = out[r["sym"]].get(d, 0) + v
    return out


def _window_sum(m: dict, dates: list, i: int, back: int) -> float:
    """按 dates[i] 往前 back 个交易日累加。"""
    lo = max(0, i - back + 1)
    return float(sum(m.get(d, 0) for d in dates[lo:i + 1]))


def build_train_set(con: sqlite3.Connection) -> dict:
    """构建训练集：采样日 × 候选票 → {5 维特征} + y（T+5 超额）。

    特征定义（口径写进 payload.caliber，可追溯）：
      blogger  = 采样日往前 20 个交易日内，该票被博主提及的**信誉加权声量**（strength 之和 /100）
      strategy = 采样日当天命中的**选股类**战法个数
      senti    = 采样日往前 10 个交易日内，论坛情绪净量（看多-看空）
      engine   = 采样日往前 5 个交易日内，主引擎/四支柱提名次数
      lhb      = 采样日往前 3 个交易日内，龙虎榜信号方向分（正值-负值，仅供诊断）
      y        = 从采样日收盘买入、持有 5 个交易日的**超额收益**（相对全市场等权中位数）
    """
    _STATUS.update(stage="载入 K 线底座", detail="", at=_now())
    bars = resonance.load_bars()
    if not bars:
        return {"ok": False, "error": "本地 K 线缓存为空"}
    # ⚠️ 必须把 K 线回填给 resonance 的模块级 _BARS（2026-10-15 深度审计发现的真 bug）：
    # resonance._latest_trade_date() 靠 _BARS 判定交易日；这里不设，它就会退化成
    # 自然日（今天），导致 evo_weight.fit_date / evo_journal.log_date 比
    # res_stock_card.build_date 多一天 —— 同一批数据出现两个日期，就是口径漂移。
    resonance._set_bars(bars)
    idx, bench = resonance.build_indices(bars)

    all_dates = sorted([d for d, v in bench.items() if v is not None])
    if len(all_dates) < SAMPLE_DAYS + 8:
        return {"ok": False, "error": "交易日不足（%d 天）" % len(all_dates)}
    sample_dates = all_dates[-(SAMPLE_DAYS + 6):-6]      # 尾部留 6 天给 T+5

    codes = {r["code"] for r in con.execute(
        "SELECT DISTINCT code FROM sig_unified WHERE code <> ''")}
    codes |= {r["symbol"] for r in con.execute("SELECT symbol FROM watch_items")}
    codes = {c for c in codes if c}
    if not codes:
        return {"ok": False, "error": "无候选票（sig_unified 为空？）"}

    _STATUS.update(stage="战法历史回放", detail="%d 只候选票" % len(codes), at=_now())
    strat_daily = _strategy_daily(codes)
    senti_d = _senti_daily(con)

    # 博主 / 引擎 / 龙虎榜信号（来自统一信号总线，带 strength）
    sig: dict = {}
    for r in con.execute("SELECT ts,src_type,code,direction,strength FROM sig_unified"):
        sig.setdefault((r["code"], r["ts"]), []).append(
            (r["src_type"], r["direction"], r["strength"] or 0.0))

    rows = []
    for code in codes:
        b = bars.get(code) or []
        m = idx.get(code) or {}
        dseq = [x[0] for x in b]
        for d in sample_dates:
            i0 = m.get(d)
            if i0 is None or i0 + 6 >= len(b):
                continue
            # ★ P1-a 时间对齐（dy8 2026-10-21，进哥拍板）：信号在采样日收盘后才可见，
            # 实际只能**次日**买入。原口径按采样日收盘价买入＝用了买不到的价格（虚增成绩）。
            # 实证：blogger 信号 D 日收盘买入 T+5 超额 +0.593%，D+1 买入 -0.484%，差 1.08pct。
            bi = i0 + 1
            base = b[bi][1]
            fut = b[bi + 5][1]
            if not base or not fut:
                continue
            ex = (fut / base - 1.0) * 100.0
            bs = 0.0
            for k in range(bi + 1, bi + 6):
                bm = bench.get(dseq[k]) if k < len(dseq) else None
                if bm is None:
                    bs = None
                    break
                bs += bm
            if bs is None:
                continue
            y = round(ex - bs, 3)
            # 窗口索引（dates 用票自身序列）
            j = dseq.index(d)
            bw, eng, lhb, sn = 0.0, 0, 0.0, 0.0
            for k in range(max(0, j - 19), j + 1):
                for (st, dr, sv) in sig.get((code, dseq[k]), []):
                    if st == "blogger":
                        bw += sv / 100.0
            for k in range(max(0, j - 4), j + 1):
                for (st, dr, sv) in sig.get((code, dseq[k]), []):
                    if st == "engine":
                        eng += 1
            for k in range(max(0, j - 2), j + 1):
                for (st, dr, sv) in sig.get((code, dseq[k]), []):
                    if st == "lhb":
                        lhb += 1.0 if dr == "多" else (-1.0 if dr == "空" else 0.0)
            sd = senti_d.get(code) or {}
            if sd:
                lo = max(0, j - 9)
                sn = float(sum(sd.get(dseq[k], 0) for k in range(lo, j + 1)))
            rows.append({
                "code": code, "date": d, "y": y,
                "f": {"blogger": round(bw, 3),
                      "strategy": float((strat_daily.get(code) or {}).get(d, 0)),
                      "senti": sn, "engine": float(eng), "lhb": lhb},
            })
    mkt_of = {}
    basis_n = {}
    try:
        # 2026-10-30 P1-a：`market_state_log` 有两个写入方（pillars / resonance），
        #   同一列两种规则。这里显式把 basis 读出来并在返回体里报数 —— 只报告，不改变
        #   任何分组口径（`by_state` 仍按 state 分，重量级结论等 P1-b 再议）。
        from viewer import state_basis as _SB
        for _x in _SB.rows(con):
            mkt_of[_x["trade_date"]] = _x["state"]
            basis_n[_x["basis"]] = basis_n.get(_x["basis"], 0) + 1
    except Exception:  # noqa: BLE001
        pass
    for r in rows:
        r["mkt"] = mkt_of.get(r["date"], "未知")
    return {"ok": True, "rows": rows, "sample_dates": sample_dates,
            "codes": len(codes), "strat_codes": len(strat_daily),
            "state_basis": basis_n}


# ================================================================ ① 权重自学习
def learn_weights(rows: list) -> dict:
    """IC 加权法：权重 ∝ max(0, IC)，IC 带样本量收缩。

    权重学已收口到 metalearn.weights_from_ic（单一真源，与 pillars / 量化板共用）；
    本函数只负责：① 从 rows 收集各维 IC 与样本量 ② 调用共享引擎 ③ 保留分市场环境的
    by_state（用**原始 IC**，不收缩，单独口径，不进主权重）。

    为什么不用最小二乘回归：样本里各维度覆盖率极不均衡（战法几千行、博主几百行），
    普通回归会被高覆盖维度主导；IC 加权对每个维度**独立评估**，公平且可解释。
    """
    ic_raw, ic_adj, n_by_dim = {}, {}, {}
    for dim in DIMS + AUX_DIMS:
        xs, ys = [], []
        for r in rows:
            v = r["f"].get(dim)
            if v is None:
                continue
            xs.append(float(v))
            ys.append(float(r["y"]))
        n = len(xs)
        n_by_dim[dim] = n
        ic = _pearson(xs, ys) if n >= MIN_IC_N else None
        ic_raw[dim] = round(ic, 4) if ic is not None else None
        ic_adj[dim] = round(ic * (n / (n + IC_SHRINK_K)), 4) if ic is not None else None
    # 主权重（共享引擎）：T+5 主轴，dims=DIMS
    # P0 显著性门槛（dy8）：|t|<2 的维度权重封顶 20%，防统计无效源长期占高权
    w, shrunk, learned, has_shrunk, note = weights_from_ic(
        {d: ic_raw.get(d) for d in DIMS},
        {d: n_by_dim.get(d, 0) for d in DIMS},
        DIMS, DEFAULT_W, t_min=2.0, insig_cap=0.20)
    # 分市场环境学一套（样本够才算，否则记 None）—— 用原始 IC（不收缩），单独口径
    by_state = {}
    for st in ("结构", "震荡", "普跌"):
        sub = [r for r in rows if r["mkt"] == st]
        if len(sub) < MIN_IC_N * 3:
            continue
        p, ic2 = {}, {}
        for d in DIMS:
            xs = [float(r["f"].get(d, 0)) for r in sub]
            ys2 = [float(r["y"]) for r in sub]
            ic2[d] = _pearson(xs, ys2)
            p[d] = max(0.0, ic2[d] or 0.0)
        t2 = sum(p.values())
        if t2 > 1e-9:
            lr = {d: v / t2 for d, v in p.items()}
            mx = {d: (1.0 - W_PRIOR_MIX) * lr[d] + W_PRIOR_MIX * DEFAULT_W[d] for d in DIMS}
            for d in DIMS:
                if mx[d] < W_FLOOR:
                    mx[d] = W_FLOOR
            s2 = sum(mx.values())
            ws = {d: round(mx[d] / s2, 4) for d in DIMS}
        else:
            ws = dict(DEFAULT_W)
        by_state[st] = {"n": len(sub), "w": ws,
                        "ic": {d: (round(ic2[d], 4) if ic2[d] is not None else None) for d in DIMS}}
    return {"ic": ic_raw, "ic_adj": ic_adj, "w": w, "by_state": by_state, "note": note,
            "n": len(rows)}


# ================================================================ ①-b 滚动样本外验证
def verify_oos(rows: list) -> dict:
    """滚动样本外验证（dy8 2026-10-21 进哥要求"持续地做"）。

    目的：不只看"全样本 IC"，还要看**时间上是否稳定**——样本内好、样本外崩，
    就是在过拟合。做法：按日期排序后 70/30 切分，分别算各维 IC 与组合分 IC。

    返回 {"in": {...}, "out": {...}, "decay": {...}, "split": 日期}，写进
    evo_weight.oos_json，每日一行，可看趋势。
    """
    if not rows:
        return {}
    rs = sorted(rows, key=lambda r: r.get("date") or "")
    cut = int(len(rs) * 0.7)
    if cut < 50 or len(rs) - cut < 50:
        return {"skip": "样本不足以切分（%d 行）" % len(rs)}
    segs = {"in": rs[:cut], "out": rs[cut:]}
    res = {"split": "%s..%s | %s..%s" % (segs["in"][0]["date"], segs["in"][-1]["date"],
                                          segs["out"][0]["date"], segs["out"][-1]["date"]),
           "n_in": len(segs["in"]), "n_out": len(segs["out"])}
    for name, sub in segs.items():
        ys = [float(r["y"]) for r in sub]
        d = {}
        for dim in DIMS:
            xs = [float(r["f"].get(dim, 0) or 0) for r in sub]
            ic = _pearson(xs, ys)
            d[dim] = round(ic, 4) if ic is not None else None
        res[name] = d
        res.setdefault("mean_y", {})[name] = round(_mean(ys), 3)
    res["decay"] = {k: (round(res["out"][k] - res["in"][k], 4)
                        if res["in"].get(k) is not None and res["out"].get(k) is not None else None)
                    for k in DIMS}
    return res


# ================================================================ ② 规则挖掘
def mine_rules(rows: list) -> list:
    """组合空间枚举 + 时间序 70/30 样本外验证。

    条件空间：环境(3) × 博主声量(3) × 战法命中(3) × 舆情(3) × 龙虎榜(3) = 243 组。
    这些组合没有任何一位博主说过 —— 是从数据里自己长出来的。
    """
    if not rows:
        return []
    baseline = _mean([r["y"] for r in rows])
    groups: dict = {}
    for r in rows:
        key = (r.get("mkt") or "未知", _bucket_blogger(r["f"]["blogger"]),
               _bucket_strat(r["f"]["strategy"]), _bucket_senti(r["f"]["senti"]),
               _bucket_lhb(r["f"]["lhb"]))
        groups.setdefault(key, []).append(r)
    out = []
    for key, arr in groups.items():
        if len(arr) < MIN_RULE_N:
            continue
        arr_sorted = sorted(arr, key=lambda x: x["date"])
        cut = int(len(arr_sorted) * OOS_SPLIT)
        tr = arr_sorted[:cut]
        oos = arr_sorted[cut:]
        if len(tr) < 10:
            continue
        tr_y = [x["y"] for x in tr]
        oos_y = [x["y"] for x in oos] if oos else []
        avg = _mean([x["y"] for x in arr])
        oos_avg = _mean(oos_y) if oos_y else None
        lift = avg - baseline
        if lift < RULE_LIFT_MIN:
            continue
        st = ("已采纳" if (oos_avg is not None and oos_avg > RULE_ADOPT_OOS and len(oos_y) >= 8)
              else "候选")
        cond_text = ("环境=%s ＋ 博主声量=%s ＋ 战法命中=%s ＋ 舆情=%s ＋ 龙虎榜=%s"
                     % (key[0], key[1], key[2], key[3], key[4]))
        out.append({
            "rid": "R" + hashlib.sha1(cond_text.encode("utf-8")).hexdigest()[:8].upper(),
            "cond": {"mkt": key[0], "blogger": key[1], "strat": key[2],
                     "senti": key[3], "lhb": key[4]},
            "cond_text": cond_text,
            "n": len(arr), "win_rate": round(_win_rate([x["y"] for x in arr]), 3),
            "avg_ex": round(avg, 2), "baseline": round(baseline, 2), "lift": round(lift, 2),
            "oos_n": len(oos_y), "oos_avg_ex": (round(oos_avg, 2) if oos_avg is not None else None),
            "oos_win": (round(_win_rate(oos_y), 3) if oos_y else None),
            "status": st,
        })
    out.sort(key=lambda x: (-x["lift"], -x["n"]))
    return out[:28]


# ================================================================ ③ 衰减与信誉进化
def detect_trends(con: sqlite3.Connection, bars: dict) -> list:
    """每个信源：近 RECENT_DAYS 个交易日超额 vs 全期超额 → 上升/稳定/衰减。"""
    idx, bench = resonance.build_indices(bars)
    all_dates = sorted([d for d, v in bench.items() if v is not None])
    recent_from = all_dates[-RECENT_DAYS] if len(all_dates) > RECENT_DAYS else ""
    rows = con.execute("SELECT src_type,src_key,code,ts,ex_t5 FROM sig_unified "
                       "WHERE fill_status='ok' AND ex_t5 IS NOT NULL").fetchall()
    g: dict = {}
    for r in rows:
        k = (r["src_type"], r["src_key"])
        a = g.setdefault(k, {"all": [], "recent": []})
        a["all"].append(r["ex_t5"])
        if recent_from and r["ts"] >= recent_from:
            a["recent"].append(r["ex_t5"])
    out = []
    for (st, sk), a in g.items():
        if len(a["all"]) < 5:
            continue
        ex_all = _mean(a["all"])
        ex_rec = _mean(a["recent"]) if len(a["recent"]) >= 5 else None
        delta = (ex_rec - ex_all) if ex_rec is not None else None
        if delta is None:
            tr = "样本不足"
        elif delta > TREND_EPS:
            tr = "上升"
        elif delta < -TREND_EPS:
            tr = "衰减"
        else:
            tr = "稳定"
        out.append({"src_type": st, "src_key": sk, "ex_all": round(ex_all, 2),
                    "ex_recent": (round(ex_rec, 2) if ex_rec is not None else None),
                    "delta": (round(delta, 2) if delta is not None else None),
                    "trend": tr, "n_all": len(a["all"]), "n_recent": len(a["recent"])})
    out.sort(key=lambda x: (x["delta"] if x["delta"] is not None else -999), reverse=True)
    return out


# ================================================================ ④ 知识内化
def internalize(con: sqlite3.Connection, rules: list, weights: dict, trends: list) -> list:
    """把挖掘结果 + 图谱互证，写成**我们自己的方法论条目**（不是别人的原话）。"""
    out = []
    adopt = [r for r in rules if r["status"] == "已采纳"]
    # 1) 从挖掘出的规则 → 结论型知识
    for i, r in enumerate(adopt[:10]):
        out.append({
            "kid": "K-RULE-%02d" % i, "kind": "结论",
            "title": "数据自生长规则：%s" % r["cond_text"],
            "body": ("当「%s」同时成立时，买入选中的个股并持有 5 个交易日，"
                     "平均超额 %.2f%%（全样本基线 %.2f%%，提升 %.2f 个百分点），"
                     "样本外（后 30%%）平均超额 %s%%、胜率 %s。"
                     "该组合不属于任何单一博主或战法的口径，是从历史数据中自己长出来的。"
                     % (r["cond_text"], r["avg_ex"], r["baseline"], r["lift"],
                        r["oos_avg_ex"], r["oos_win"])),
            "evidence": "样本 %d 条（训练 %d / 验证 %d），胜率 %.1f%%"
                        % (r["n"], int(r["n"] * OOS_SPLIT), r["oos_n"], r["win_rate"] * 100),
            "sources": "evo_rule 自挖掘｜sig_unified 六源信号｜全市场等权基准",
            "status": "已验证",
        })
    # 2) 从权重学习 → 方法型知识（这条最核心：数据推翻了人的直觉）
    w = weights.get("w") or {}
    ic = weights.get("ic") or {}
    bl = w.get("blogger")
    icb = ic.get("blogger")
    if bl is not None:
        if icb is not None and icb < 0:
            verdict = ("⚠️ **反直觉结论**：博主声量的信息系数为 %.4f（负），即"
                       "**声量越高、未来 T+5 超额反而越低** —— 该维度当前更像「拥挤度/热度」"
                       "指标而非买入信号（多位博主扎堆推荐，往往意味着短期已经涨过头）。"
                       "权重据此被压到 %.1f%%。" % (icb, bl * 100))
        elif bl < 0.12:
            verdict = ("数据判定博主声量的独立预测力很弱，权重被压到 %.1f%% —— "
                       "与「多位博主共同推荐就该重仓」的直觉相反。" % (bl * 100))
        elif bl < DEFAULT_W["blogger"]:
            verdict = "博主声量权重 %.1f%%，低于默认 %.0f%%，说明其独立贡献有限。" % (
                bl * 100, DEFAULT_W["blogger"] * 100)
        else:
            verdict = "博主声量权重 %.1f%%，不低于默认值，本窗口其独立贡献成立。" % (bl * 100)
        out.append({
            "kid": "K-WEIGHT", "kind": "方法",
            "title": "共振分权重改由数据决定（不再由人拍板）",
            "body": ("本期按信息系数重新学习：博主 %.1f%% / 战法 %.1f%% / 舆情 %.1f%% / 主引擎 %.1f%%。%s "
                     "IC 一览：%s。IC ≤ 0 的维度由先验收缩压到默认权重的一半以下，"
                     "再受 %.0f%% 单维度下限保护 —— 既让数据说话，又不让多维融合塌缩成单维。"
                     % (w.get("blogger", 0) * 100, w.get("strategy", 0) * 100,
                        w.get("senti", 0) * 100, w.get("engine", 0) * 100, verdict,
                        json.dumps(weights.get("ic_adj") or {}, ensure_ascii=False),
                        W_FLOOR * 100)),
            "evidence": "训练样本 %d 条，IC 经样本量收缩（n/(n+%.0f)）" % (weights.get("n", 0), IC_SHRINK_K),
            "sources": "sig_unified 已回填信号 × 全市场等权基准",
            "status": "已验证",
        })
    # 3) 从衰减 → 观察型知识
    dec = [t for t in trends if t["trend"] == "衰减"][:3]
    for i, t in enumerate(dec):
        out.append({
            "kid": "K-DECAY-%02d" % i, "kind": "观察",
            "title": "信源退步预警：%s" % t["src_key"],
            "body": ("该源全期平均超额 %.2f%%，近 %d 个交易日降至 %.2f%%，变化 %.2f 个百分点。"
                     "系统自动标记为「衰减」——它的历史战绩不再代表近期能力，参考时应降权。"
                     % (t["ex_all"], RECENT_DAYS, t["ex_recent"] or 0,
                        t["delta"] or 0)),
            "evidence": "全期样本 %d 条 / 近期样本 %d 条" % (t["n_all"], t["n_recent"]),
            "sources": "sig_unified 回填 × 滚动窗口对比",
            "status": "观察中",
        })
    # 4) 从知识图谱的「存疑」→ 反例型知识
    try:
        for i, r in enumerate(con.execute(
                "SELECT title, track, verdict FROM kb_strategy_node WHERE consistency='存疑' LIMIT 3")):
            out.append({
                "kid": "K-COUNTER-%02d" % i, "kind": "反例",
                "title": "被回测推翻的说法：%s" % (r["title"] or "")[:36],
                "body": ("知识轨道「%s」中的这条说法，在本系统回测里未能成立：%s。"
                         "记录为反例，避免照搬。" % (r["track"] or "", r["verdict"] or "")),
                "evidence": "跨源互证：视频语 / 文档 ↔ strategy_sim 回测",
                "sources": "kb_strategy_node（图谱互证）",
                "status": "已验证",
            })
    except Exception:  # noqa: BLE001
        pass
    return out


# ================================================================ 主流程
def learn(con: sqlite3.Connection, force: bool = False) -> dict:
    with _LOCK:
        t0 = time.time()
        ensure_tables(con)
        try:
            con.execute("PRAGMA busy_timeout=90000")
        except Exception:  # noqa: BLE001
            pass
        ts = build_train_set(con)
        if not ts.get("ok"):
            return {"ok": False, "error": ts.get("error")}
        rows = ts["rows"]
        if len(rows) < MIN_IC_N:
            return {"ok": False, "error": "训练样本不足（%d 行）" % len(rows)}

        # ★ 自愈清理（2026-10-15 深度审计发现）：早期版本里 fit_date 会漂到**自然日**
        # （因 resonance._BARS 未被回填），于是同一批数据留下两行；而
        # `_load_weights` 按 fit_date 倒序取最新 → 会一直读到那行**未做精度校准**的旧权重。
        # 这里在学之前把「晚于最新交易日」的脏行清掉，保证日期口径唯一。
        try:
            tdx = resonance._latest_trade_date()
            if tdx:
                con.execute("DELETE FROM evo_weight WHERE fit_date > ?", (tdx,))
                con.execute("DELETE FROM evo_journal WHERE log_date > ?", (tdx,))
                con.commit()
        except Exception:  # noqa: BLE001
            pass

        _STATUS.update(stage="权重自学习", detail="%d 行样本" % len(rows), at=_now())
        weights = learn_weights(rows)
        # dy8 P1：滚动样本外验证（持续记录，看模型是否在漂）
        try:
            oos = verify_oos(rows)
        except Exception as e:  # noqa: BLE001
            oos = {"error": str(e)}

        _STATUS.update(stage="规则挖掘", detail="", at=_now())
        rules = mine_rules(rows)

        _STATUS.update(stage="衰减与信誉进化", detail="", at=_now())
        bars = resonance.load_bars()
        trends = detect_trends(con, bars)

        _STATUS.update(stage="知识内化", detail="", at=_now())
        knowledge = internalize(con, rules, weights, trends)

        fit_date = resonance._latest_trade_date()
        prev_w = None
        try:
            r = con.execute("SELECT * FROM evo_weight WHERE fit_date < ? ORDER BY fit_date DESC LIMIT 1",
                            (fit_date,)).fetchone()
            if r:
                prev_w = {"blogger": r["w_blogger"], "strategy": r["w_strategy"],
                          "senti": r["w_senti"], "engine": r["w_engine"]}
        except Exception:  # noqa: BLE001
            pass
        prev_rules = set()
        try:
            prev_rules = {r["rid"] for r in con.execute("SELECT rid FROM evo_rule")}
        except Exception:  # noqa: BLE001
            pass

        w = weights["w"]
        try:
            con.execute("ALTER TABLE evo_weight ADD COLUMN oos_json TEXT")
        except Exception:  # noqa: BLE001  列已存在
            pass
        _oos_txt = None
        try:
            _oos_txt = json.dumps(oos, ensure_ascii=False)
            _od = "、".join("%s %+.3f→%+.3f" % (k, oos["in"][k] or 0, oos["out"][k] or 0)
                            for k in DIMS) if oos.get("in") else ""
            if _od:
                weights["note"] += "｜滚动样本外(%d/%d 切分)：%s" % (
                    oos.get("n_in", 0), oos.get("n_out", 0), _od)
        except Exception:  # noqa: BLE001
            pass
        con.execute(
            "INSERT OR REPLACE INTO evo_weight(fit_date,n_rows,ic_json,w_json,w_default_json,"
            "by_state_json,w_blogger,w_strategy,w_senti,w_engine,note,created_at,oos_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fit_date, weights["n"], json.dumps(weights["ic"], ensure_ascii=False),
             json.dumps(w, ensure_ascii=False), json.dumps(DEFAULT_W, ensure_ascii=False),
             json.dumps(weights["by_state"], ensure_ascii=False),
             w["blogger"], w["strategy"], w["senti"], w["engine"], weights["note"], _now(),
             _oos_txt))

        con.execute("DELETE FROM evo_rule")
        for r in rules:
            con.execute(
                "INSERT OR REPLACE INTO evo_rule(rid,cond_json,cond_text,n,win_rate,avg_ex,"
                "baseline,lift,oos_n,oos_avg_ex,oos_win,status,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["rid"], json.dumps(r["cond"], ensure_ascii=False), r["cond_text"], r["n"],
                 r["win_rate"], r["avg_ex"], r["baseline"], r["lift"], r["oos_n"],
                 r["oos_avg_ex"], r["oos_win"], r["status"], _now()))

        con.execute("DELETE FROM evo_knowledge")
        for k in knowledge:
            con.execute(
                "INSERT OR REPLACE INTO evo_knowledge(kid,kind,title,body,evidence,sources,"
                "status,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (k["kid"], k["kind"], k["title"], k["body"], k["evidence"], k["sources"],
                 k["status"], _now()))

        con.execute("DELETE FROM evo_source_trend")
        for t in trends:
            con.execute(
                "INSERT OR REPLACE INTO evo_source_trend(src_type,src_key,ex_all,ex_recent,delta,"
                "trend,n_all,n_recent,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (t["src_type"], t["src_key"], t["ex_all"], t["ex_recent"], t["delta"],
                 t["trend"], t["n_all"], t["n_recent"], _now()))

        # 每日进化日志
        delta_w = {}
        if prev_w:
            for d in DIMS:
                delta_w[d] = round((w[d] - (prev_w.get(d) or 0)) * 100, 2)
        new_rules = [r for r in rules if r["rid"] not in prev_rules]
        dead = [r for r in rules if r["status"] != "已采纳" and r["lift"] < 0]
        moves = [t for t in trends if t["delta"] is not None][:3]
        adopt_n = sum(1 for r in rules if r["status"] == "已采纳")
        learned_txt = (
            "本期从 %d 条样本中学出：权重 博主 %.1f%% / 战法 %.1f%% / 舆情 %.1f%% / 主引擎 %.1f%%%s；"
            "自生长规则 %d 条（其中样本外通过、已采纳 %d 条）；信源衰减预警 %d 个；"
            "内化知识条目 %d 条。"
            % (weights["n"], w["blogger"] * 100, w["strategy"] * 100, w["senti"] * 100,
               w["engine"] * 100,
               ("；相对上期变动 " + json.dumps(delta_w, ensure_ascii=False)) if delta_w else "",
               len(rules), adopt_n, len([t for t in trends if t["trend"] == "衰减"]), len(knowledge)))
        con.execute(
            "INSERT OR REPLACE INTO evo_journal(log_date,learned,weight_json,weight_delta_json,"
            "rules_new,rules_dead,src_moves,stats_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (fit_date, learned_txt, json.dumps(w, ensure_ascii=False),
             json.dumps(delta_w, ensure_ascii=False), len(new_rules), len(dead),
             json.dumps([{"src": t["src_key"], "delta": t["delta"], "trend": t["trend"]}
                         for t in moves], ensure_ascii=False),
             json.dumps({"rows": weights["n"], "rules": len(rules), "adopted": adopt_n,
                         "knowledge": len(knowledge), "trends": len(trends)},
                        ensure_ascii=False), _now()))
        con.commit()

        # ★ 反哺：学完当场用新权重重算共振卡，让学习当天就改变产出
        #   传入已载入的 bars —— refresh_cards 需要它来定「最新交易日」，
        #   不传会退化成自然日、与分歧预警表对不上（2026-10-15 实测踩到）。
        _STATUS.update(stage="反哺共振卡（用新权重）", detail="", at=_now())
        try:
            rb = resonance.refresh_cards(con, bars)
        except Exception as e:  # noqa: BLE001
            rb = {"ok": False, "error": str(e)[:200]}

        # ★ 成果出口（2026-10-15 进哥要求「学完得有成果」）：用刚学出的权重生成「系统自选」，
        #   即由系统自己选出的当日推荐 —— 学习不再只是"展示学过什么"，而是直接产出可验证的判断。
        #   放在 refresh_cards 之后：两者都吃同一套新权重，顺序无关；这里紧跟其后便于对比。
        _STATUS.update(stage="生成系统自选（成果出口）", detail="", at=_now())
        try:
            import selfpick            # 函数内导入：selfpick 反向依赖 evolve，避免循环导入
            sp = selfpick.build(con, bars)
        except Exception as e:  # noqa: BLE001
            sp = {"ok": False, "error": str(e)[:200]}

        _STATUS.update(stage="完成", detail="", at=_now())
        return {"ok": True, "engine_ver": ENGINE_VER, "fit_date": fit_date,
                "train_rows": weights["n"], "weights": w, "ic": weights["ic"],
                "ic_adj": weights["ic_adj"], "weights_note": weights["note"],
                "by_state": list(weights["by_state"].keys()),
                "self_pick": sp,
                "rules": len(rules), "rules_adopted": adopt_n,
                "knowledge": len(knowledge), "trends": len(trends),
                "refresh_cards": rb, "elapsed": round(time.time() - t0, 1)}


# ================================================================ 只读接口
def payload(con: sqlite3.Connection) -> dict:
    ensure_tables(con)
    r = con.execute("SELECT * FROM evo_weight WHERE fit_date <= ? "
                    "ORDER BY fit_date DESC LIMIT 1",
                    (resonance._latest_trade_date(),)).fetchone()
    if not r:
        return {"ok": False, "note": "自我进化引擎尚未生成任何一期（等调度，或点「立即进化」）",
                "engine_ver": ENGINE_VER}
    weight = {"fit_date": r["fit_date"], "n_rows": r["n_rows"],
              "ic": json.loads(r["ic_json"] or "{}"),
              "w": json.loads(r["w_json"] or "{}"),
              "w_default": json.loads(r["w_default_json"] or "{}"),
              "by_state": json.loads(r["by_state_json"] or "{}"),
              "note": r["note"]}
    rules = [dict(x) for x in con.execute(
        "SELECT * FROM evo_rule ORDER BY CASE status WHEN '已采纳' THEN 0 WHEN '候选' THEN 1 ELSE 2 END,"
        " lift DESC LIMIT 30")]
    for x in rules:
        try:
            x["cond"] = json.loads(x.pop("cond_json") or "{}")
        except Exception:  # noqa: BLE001
            x["cond"] = {}
    knowledge = [dict(x) for x in con.execute(
        "SELECT * FROM evo_knowledge ORDER BY CASE kind WHEN '方法' THEN 0 WHEN '结论' THEN 1 "
        "WHEN '反例' THEN 2 ELSE 3 END LIMIT 40")]
    journal = [dict(x) for x in con.execute(
        "SELECT * FROM evo_journal ORDER BY log_date DESC LIMIT 10")]
    for x in journal:
        for k, kk in (("weight_json", "weight"), ("weight_delta_json", "delta"),
                      ("src_moves", "moves"), ("stats_json", "stats")):
            try:
                x[kk] = json.loads(x.pop(k) or "{}")
            except Exception:  # noqa: BLE001
                x[kk] = {}
    trends = [dict(x) for x in con.execute(
        "SELECT * FROM evo_source_trend ORDER BY CASE trend WHEN '上升' THEN 0 WHEN '稳定' THEN 1 "
        "WHEN '衰减' THEN 2 ELSE 3 END, delta DESC LIMIT 60")]
    st = {}
    try:
        st = {"rules": con.execute("SELECT COUNT(*) n FROM evo_rule").fetchone()["n"],
              "adopted": con.execute("SELECT COUNT(*) n FROM evo_rule WHERE status='已采纳'").fetchone()["n"],
              "knowledge": con.execute("SELECT COUNT(*) n FROM evo_knowledge").fetchone()["n"],
              "trends": con.execute("SELECT COUNT(*) n FROM evo_source_trend").fetchone()["n"],
              "up": con.execute("SELECT COUNT(*) n FROM evo_source_trend WHERE trend='上升'").fetchone()["n"],
              "decay": con.execute("SELECT COUNT(*) n FROM evo_source_trend WHERE trend='衰减'").fetchone()["n"],
              "journal_days": con.execute("SELECT COUNT(*) n FROM evo_journal").fetchone()["n"]}
    except Exception:  # noqa: BLE001
        pass
    caliber = [
        "口径1 · 权重**由数据学出**：对每个维度计算信息系数 IC（该维度取值 与 未来 T+5 超额 的相关系数），"
        "权重 ∝ max(0, IC)，IC 经样本量收缩 n/(n+%.0f)；再经 %.0f%% 先验收缩（保留一部分默认权重）"
        "与 %.0f%% 单维下限，避免单期数据把多维融合塌缩成单维。" % (IC_SHRINK_K, W_PRIOR_MIX * 100, W_FLOOR * 100),
        "口径2 · 特征定义：博主=近 20 交易日信誉加权声量；战法=当日命中的选股类战法数（择时类已剔除）；"
        "舆情=近 10 交易日看多减看空；主引擎=近 5 交易日提名次数；y=买入持有 5 日的超额收益"
        "（相对全市场等权中位数）。",
        "口径3 · 规则挖掘用**时间序 70/30 切分**做样本外验证（不用随机切分，避免未来函数）；"
        "样本外平均超额 > 0 且验证样本 ≥ 8 才晋升「已采纳」。",
        "口径4 · 龙虎榜**只做诊断展示**（看它的 IC 是正是负），不进共振权重 —— "
        "遵循「上榜是结果不是原因」的一贯口径。",
        "口径5 · 衰减检测：近 %d 个交易日平均超额 vs 全期平均超额，差 > %.1f 个百分点才判「上升/衰减」，"
        "否则记「稳定」；近期样本不足 5 条记「样本不足」。" % (RECENT_DAYS, TREND_EPS),
        "口径6 · 学习结果**当天反哺产出**：学完立即用新权重重算共振卡（refresh_cards），"
        "不是只写进一张表放着。",
    ]
    return {"ok": True, "engine_ver": ENGINE_VER, "weight": weight, "rules": rules,
            "data_date": weight.get("fit_date"),   # 业务日期显式下发（审计器 T2 要求）
            "knowledge": knowledge, "journal": journal, "trends": trends,
            "stats": st, "caliber": caliber,
            "note": "从「汇聚别人的」到「长出自己的」：权重由数据决定、规则由数据生长、"
                    "学完当天改变产出。"}


def status_payload(con: sqlite3.Connection) -> dict:
    ensure_tables(con)
    return {"engine_ver": ENGINE_VER, "stage": _STATUS.get("stage"),
            "running": bool(_STATUS.get("running")),
            "detail": _STATUS.get("detail"), "at": _STATUS.get("at"), "today": _today(),
            "runs": [{"log_date": r["log_date"]} for r in con.execute(
                "SELECT log_date FROM evo_journal ORDER BY log_date DESC LIMIT 8")]}


def run_async(con: sqlite3.Connection, force: bool = False) -> dict:
    """后台线程跑一次学习，**立即返回**。

    为什么要异步（2026-10-15 实测踩坑）：learn() 含战法历史回放，要跑 10~20 秒；
    同步 HTTP 接口会被浏览器/网关的超时掐断 —— curl POST 直接返回
    `502 upstream connection error`。改为后台线程 + 前端轮询 status。
    """
    if _STATUS.get("running"):
        return {"ok": True, "started": False, "stage": _STATUS.get("stage") or "",
                "note": "已有一次进化正在进行，请稍候"}
    _STATUS["running"] = True

    def _job():
        try:
            learn(con, force=force)
        except Exception:  # noqa: BLE001
            logging.exception("evolve 后台进化失败（下一轮再试）")
        finally:
            _STATUS["running"] = False

    threading.Thread(target=_job, name="evolve-learn", daemon=True).start()
    return {"ok": True, "started": True,
            "note": "已启动后台进化（约 10~20 秒），完成后自动刷新"}


def maybe_daily(con: sqlite3.Connection) -> dict:
    """调度入口：当天没学过就学一次（自带锁 + 异常隔离）。"""
    try:
        row = con.execute("SELECT COUNT(*) n FROM evo_weight WHERE fit_date=?",
                          (resonance._latest_trade_date(),)).fetchone()
        if row and row["n"] > 0:
            return {"ok": True, "skipped": "今天已学习过"}
    except Exception:  # noqa: BLE001
        pass
    try:
        return learn(con)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}
