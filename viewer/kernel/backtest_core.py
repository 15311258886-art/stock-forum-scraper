# -*- coding: utf-8 -*-
"""回测内核（BacktestKernel）—— 前向收益 / 成本 / 同池基准的单一事实源。

为什么存在这个文件
    重构前，回测相关的「前向收益」「成本扣减」「规模分位」「t 值」「分段」散落在
    router_backtest.py / signal_backtest.py / ab_box_mode.py / signal_ic.py /
    factor_import.py / person_score.py 等多处，签名与口径各写一遍。任何一处改了
    成本假设，其余地方会静默漂移（这正是 T10 与 P4 信号回测一度「13bp vs 15bp」
    对不上的根因）。

本模块只放「与具体工具无关、纯函数、无副作用」的回测原语，供各引擎共用：
  · window_at          时间之墙（唯一允许接触 K 线的取数口）
  · fwd_return         给定窗口下标 → 之后 horizon 根的后复权收益
  · fwd_detail         同 fwd_return，但额外返回「实际取到收益的那一天」（T08 账本用）
  · liq_at            规模/流动性代理（后复权下 量×价 是不变量 = 真实成交额）
  · quintile_bounds   按规模排序取五分位边界
  · tstat             按日聚合后的 t 值
  · slices            区间分段（审计要求「分 6 段」）
  · apply_cost        扣双边成本（bp → 小数）

设计约束（贯穿全局，不得破坏）
    1. 时间之墙：判据只准看 <= D 的 K 线。window_at 是整个系统唯一取数口，
       它不越界，下游工具就不可能看到未来。
    2. 成本先扣：成本在「超额 = 入选均值 − 同池均值 − 成本」处一次性扣除，
       本模块不藏两套结论。
    3. 后复权口径：所有收益都基于后复权收盘对收盘，与 kbase.db 的 adj='hfq' 一致。

故意不合并的同类函数（避免「为统一而统一」引入错误）
    · ab_box_mode._fwd_ret(bars, i0, target)         —— 按「目标日期」取收益，不是按 horizon 根数，
                                                       合同不同，保留原地并注明。
    · factor_import.forward_return(mats, h)          —— numpy 向量化（整矩阵一次算），性能画像不同。
    · person_score.forward_return(self, code, date, n) —— 逐票、走 DB，I/O 合同不同。
    · signal_ic._fwd_ret(series, date, fwd)          —— 作用在收益率序列上，输入不是 K 线窗口。
    这四类若强行捏进一个签名，反而会把不同合同混为一谈。本内核只收口
    「(截断窗口, 下标, horizon) → 后复权收益」这一最通用、被 T10 审计直接使用的合同。
"""
from __future__ import annotations

import bisect
import math
import statistics

__all__ = [
    "window_at", "fwd_return", "fwd_detail", "fwd_return_open", "fwd_detail_open",
    "liq_at", "quintile_bounds", "tstat", "slices", "apply_cost",
]


def window_at(entry: dict, D: str, keep: int):
    """【时间之墙】取某只票「截止 D（含）」的最近 keep 根 K 线 → (window, idx)。

    这是整个回测里唯一允许接触 K 线的地方，判据函数只拿到它的返回值，
    所以只要这里不越界，工具就**不可能**看到未来。

    单独抽成函数是为了能被测试直接钉住（`tests/test_router_backtest.py`）：
    前视偏差不会报错、只会让每个工具都跑出漂亮超额，是最难事后发现的一类 bug。
    """
    i = bisect.bisect_right(entry["dates"], D) - 1
    if i < 0:
        return None, -1
    return entry["bars"][max(0, i - keep + 1): i + 1], i


def fwd_return(entry: dict, idx: int, horizon: int):
    """D 之后 horizon 个交易日的收益（后复权收盘对收盘）。越界返回 None。"""
    bars = entry["bars"]
    j = idx + horizon
    if idx < 0 or j >= len(bars):
        return None
    c0, c1 = bars[idx][2], bars[j][2]
    if not c0 or not c1 or c0 <= 0:
        return None
    return c1 / c0 - 1.0


def fwd_detail(entry: dict, idx: int, horizon: int):
    """同 `fwd_return`，但多返回一个「实际取到收益的那一天」。T08 账本要用它记到期日。

    为什么到期日不能直接算成「D + horizon 个自然日或交易日」：
    停牌、退市、北交所不同日历都会让实际可成交日偏移。账本里必须写**实际拿到的那天**，
    否则事后对账会以为「到期没回款」。
    """
    bars = entry["bars"]
    j = idx + horizon
    if idx < 0 or j >= len(bars):
        return None, None
    c0, c1 = bars[idx][2], bars[j][2]
    if not c0 or not c1 or c0 <= 0:
        return None, None
    return c1 / c0 - 1.0, str(bars[j][0])


def fwd_return_open(entry: dict, idx: int, horizon: int):
    """T+1 开盘入场 → T+1+horizon 开盘出场（可执行口径，无前视）。

    与 `fwd_return`（D 收盘入场）的唯一区别：入场/出场都改用**开盘价**且入场日推后一天。
    这是 `signal_backtest` 经 2026-10-31 P0-C 整改后采用的口径：信号基于 T 日收盘因子，
    实际只能在 T+1 开盘成交，「用当日收盘信号在当日收盘成交」是逻辑上不可达的前视。
    见 docs/adr/D78。bars 字段顺序见 fwd_return：b[1]=open，b[2]=close。
    """
    bars = entry["bars"]
    ei = idx + 1
    xj = ei + horizon
    if ei < 0 or xj >= len(bars):
        return None
    o0, o1 = bars[ei][1], bars[xj][1]
    if not o0 or not o1 or o0 <= 0:
        return None
    return o1 / o0 - 1.0


def fwd_detail_open(entry: dict, idx: int, horizon: int):
    """同 `fwd_return_open`，但多返回实际出场日（T08 账本记到期日用）。"""
    bars = entry["bars"]
    ei = idx + 1
    xj = ei + horizon
    if ei < 0 or xj >= len(bars):
        return None, None
    o0, o1 = bars[ei][1], bars[xj][1]
    if not o0 or not o1 or o0 <= 0:
        return None, None
    return o1 / o0 - 1.0, str(bars[xj][0])


def liq_at(bars, win: int = 20):
    """规模/流动性代理：最近 `win` 根的平均成交额（元）。取不到返回 None。

    为什么不用「总股本 × 收盘价」算市值
        kbase 存的是**后复权**价（`adj='hfq'`），而股本只能取最新快照 ——
        两个近似叠在一起，误差方向不可控（历史分红多的票市值被高估）。
    为什么成交额可以
        后复权是「价 × factor、量 ÷ factor」，因此 `量 × 价` 是**不变量**，
        等于当日真实成交额，不需要任何外部数据，也不受复权基准影响。
    它是「规模/流动性」代理，**不是市值**。结论里必须这么写，
    不许把「流动性中性」说成「市值中性」。
    """
    if not bars:
        return None
    tail = bars[-win:]
    tot = 0.0
    n = 0
    for b in tail:
        v, c = b[5], b[2]
        if v and c:
            tot += float(v) * float(c)
            n += 1
    return tot / n if n >= max(3, win // 2) else None


def quintile_bounds(pairs: list):
    """按规模排序后取 4 个分位边界（五分位）。"""
    if len(pairs) < 25:
        return []
    pairs = sorted(pairs)
    n = len(pairs)
    return [pairs[int(n * k / 5)][0] for k in range(1, 5)]


def tstat(xs):
    """按日聚合后的 t 值（与 avoid.py 的检验口径一致：不按票堆样本）。"""
    if len(xs) < 3:
        return None
    m = statistics.fmean(xs)
    sd = statistics.pstdev(xs)
    if sd == 0:
        return None
    return m / (sd / math.sqrt(len(xs)))


def slices(days: list, n: int = 6) -> list:
    """把区间切成 n 段（审计要求「分 6 段」，用来看结论是否只在某一段成立）。"""
    if not days:
        return []
    size = max(1, len(days) // n)
    out = []
    for i in range(0, len(days), size):
        seg = days[i:i + size]
        if seg:
            out.append(seg)
    return out[:n]


def apply_cost(ret: float | None, cost_bp: float) -> float | None:
    """扣双边成本（bp → 小数）。ret 为 None 时原样返回。

    与 router_backtest.run 的 `excess = gross - base - cost` 口径一致：
    成本在「超额」层面一次性扣除，不在单票收益里藏两套结论。
    """
    if ret is None:
        return None
    return ret - cost_bp / 10000.0
