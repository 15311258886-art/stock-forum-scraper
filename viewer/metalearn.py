# -*- coding: utf-8 -*-
"""共享元学习引擎（metalearn，2026-10-16 进哥要求「一次性做干净」）。

单一真源：把 evolve.learn_weights 与 pillars._sl_weights 重复抄写的那套
「IC → 样本量收缩 → 先验混合 → 单维下限 → 精度收口」数学，收口到一处。

三处实例化：
  ① 每日预测 E 引擎自加权  → 候选 = 四筛选支柱
  ② 博主共振 evolve 自我进化 → 候选 = 博主维度（blogger/strategy/senti/engine）
  ③ 量化板块 量化因子的自我学习与成长 → 候选 = 因子（或因子家族）

口径常量（改动须附证据）：
  - IC_SHRINK_K = 60   IC 样本量收缩：IC_adj = IC × n/(n+K)，抑制小样本偶发相关
  - W_PRIOR_MIX = 0.40 先验收缩：保留 40% 默认权重，防单期数据过拟合 / 多维塌缩成单维
  - W_FLOOR = 0.06     单维下限 6%，保多源结构
  - MIN_IC_N = 20      算 IC 的最小有效样本（低于此 IC 记 None，不参学）

设计约束：本模块**零依赖** evolve / pillars / resonance（避免循环导入）；
只依赖标准库。三个板各自 import 本模块。
"""
from __future__ import annotations

import json
import math

# ---- 口径常量（唯一真源）----------------------------------------------------
MIN_IC_N = 20
IC_SHRINK_K = 60.0
W_PRIOR_MIX = 0.40
W_FLOOR = 0.06


def pearson(xs, ys):
    """皮尔逊相关系数；样本不足或零方差返回 None。"""
    n = len(xs)
    if n < 8:
        return None
    mx, my = sum(xs) / float(n), sum(ys) / float(n)
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    if sxx <= 1e-12 or syy <= 1e-12:
        return None
    return sxy / math.sqrt(sxx * syy)


def ic_shrink(ic, n, K=IC_SHRINK_K):
    """样本量收缩后的 IC；ic 为 None 或 n < MIN_IC_N 返回 None。"""
    if ic is None or n < MIN_IC_N:
        return None
    return ic * (n / (n + K))


def _ic_t_stat(icv, nd, series=None):
    """IC 显著性 t 值（P1-22）。

    - 提供逐期 IC 时间序列 series（len>=8）时，用 Newey-West HAC 标准误，
      对 IC 序列的自相关/异方差做长程方差校正（时序上更稳健，避免把自相关当成显著信号）。
    - 否则退回 i.i.d. 近似 t = ic*sqrt(n-2)/sqrt(1-ic^2)（零假设下 IC 标准误≈1/sqrt(n)）。
    样本不足返回 None。
    """
    if icv is None or nd < 3:
        return None
    if series and len(series) >= 8:
        return _newey_west_t(series)
    return icv * math.sqrt(nd - 2) / math.sqrt(max(1e-12, 1.0 - icv * icv))


def _newey_west_t(series):
    """Newey-West(1994) HAC t 统计量：对 IC 时间序列的自相关做长程方差校正。

    L = int(4*(T/100)^0.25)（NW 滞后阶，封顶 T-1），长程方差 = γ0 + 2·Σ_{l=1..L}(1-l/(L+1))·γl。
    t = mean / sqrt(长程方差 / T)。序列过短或零方差返回 None。
    """
    xs = [float(x) for x in series if x is not None]
    T = len(xs)
    if T < 8:
        return None
    mean = sum(xs) / T

    def _acov(lag: int) -> float:
        if lag == 0:
            return sum((x - mean) ** 2 for x in xs) / T
        return sum((xs[i] - mean) * (xs[i + lag] - mean) for i in range(T - lag)) / T

    gamma0 = _acov(0)
    if gamma0 <= 1e-12:
        return None
    L = int(4 * (T / 100.0) ** 0.25)
    L = max(1, min(L, T - 1))
    lr_var = gamma0
    for l in range(1, L + 1):
        w = 1.0 - l / (L + 1.0)
        lr_var += 2.0 * w * _acov(l)
    se = math.sqrt(max(lr_var, 0.0) / T)
    if se <= 1e-12:
        return None
    return mean / se


def weights_from_ic(ic_by_dim, n_by_dim, dims, default_w,
                    K=IC_SHRINK_K, prior_mix=W_PRIOR_MIX, floor=W_FLOOR,
                    t_min=0.0, insig_cap=None, ic_series_by_dim=None):
    """由 IC 字典学权重（单一真源，evolve 与 E 共用）。

    流程：IC → 收缩 → 只取正 → 归一 → 先验混合 → 下限 → 精度收口。
    与 evolve.learn_weights / pillars._sl_weights 原公式逐项一致。

    参数：
      ic_by_dim  : {dim: 原始 IC 或 None}（未收缩）
      n_by_dim   : {dim: 有效样本数}
      dims       : 参与权重的维度列表（如 DIMS / SELF_LEARN_PILLARS）
      default_w  : 等权先验权重（如 DEFAULT_W / DEFAULT_W_SELF）
    返回 (w, shrunk, learned, has_shrunk, note)：
      - w           : 最终权重（已精度收口，和为 1）
      - shrunk      : 各维收缩后 IC（None 表示样本不足）
      - learned     : 归一后（先验混合前）的"纯数据权重"，全不足时为 None
      - has_shrunk  : False 表示所有维度 IC 不足 → 退回等权先验 default_w
      - note        : 诚实判词（自带判据，进哥风格）
    """
    shrunk = {}
    for d in dims:
        n = n_by_dim.get(d, 0)
        ic = ic_by_dim.get(d)
        shrunk[d] = ic_shrink(ic, n, K)
    pos = {d: max(0.0, shrunk.get(d) or 0.0) for d in dims}
    tot = sum(pos.values())
    if tot <= 1e-9:
        w = dict(default_w)
        learned = None
        has_shrunk = False
        note = ("各维度 T+5 的 IC 均不足（样本/相关性不够）→ 退回等权先验，并如实标注；"
                "这本身就是一条结论：当前各来源对 T+5 超额暂无独立预测力，不硬凑权重。")
        return w, shrunk, learned, has_shrunk, note
    learned = {d: round(pos[d] / tot, 4) for d in dims}
    mixed = {d: (1.0 - prior_mix) * learned[d] + prior_mix * default_w[d] for d in dims}
    for d in dims:
        if mixed[d] < floor:
            mixed[d] = floor
    # ★ P0 显著性门槛（dy8 2026-10-21，进哥拍板）：t 检验 |t|<t_min 的维度
    # 视为"统计上无效"，权重封顶 insig_cap，防止 IC≈0 的源长期占高权（堆因子陷阱）。
    # t = IC*sqrt(n-2)/sqrt(1-IC^2)（零假设下 IC 标准误 ≈ 1/sqrt(n)）。
    # 不改变返回结构（三方共用），只把 t 值写进 note 供追溯。
    _tvals = {}
    if t_min and insig_cap is not None:
        for d in dims:
            icv = ic_by_dim.get(d)
            nd = n_by_dim.get(d, 0)
            series = (ic_series_by_dim or {}).get(d) if ic_series_by_dim else None
            _tvals[d] = _ic_t_stat(icv, nd, series)
        for d in dims:
            tv = _tvals.get(d)
            if tv is None or abs(tv) < t_min:
                if mixed[d] > insig_cap:
                    mixed[d] = insig_cap
    s = sum(mixed.values())
    w = {d: round(mixed[d] / s, 4) for d in dims}
    # 精度收口（防四舍五入后权重和≠1、某维跌破下限，如 senti 变 0.0598）：
    top = max(w, key=lambda k: w[k])
    drift = round(1.0 - sum(w.values()), 4)
    w[top] = round(w[top] + drift, 4)
    for d in dims:
        if w[d] < floor:
            gap = round(floor - w[d], 4)
            w[d] = floor
            w[top] = round(w[top] - gap, 4)
    has_shrunk = True
    note = ("数据学得 IC（T+5 主轴）：%s → 经 %.0f%% 先验收缩 + 单维下限 %.0f%% → 最终 %s"
            % (json.dumps({k: (round(v, 3) if v is not None else None)
                           for k, v in ic_by_dim.items()}, ensure_ascii=False),
               prior_mix * 100, floor * 100, json.dumps(w, ensure_ascii=False)))
    if _tvals:
        _sig = []
        for d in dims:
            tv = _tvals.get(d)
            if tv is None:
                _sig.append("%s t=NA(样本不足)" % d)
            elif abs(tv) < t_min:
                _sig.append("%s t=%+.2f 不显著→封顶%.0f%%" % (d, tv, (insig_cap or 0) * 100))
            else:
                _sig.append("%s t=%+.2f 显著" % (d, tv))
        note += "｜P0 显著性（|t|≥%.1f 才算有效）：%s" % (t_min, "、".join(_sig))
    return w, shrunk, learned, has_shrunk, note
