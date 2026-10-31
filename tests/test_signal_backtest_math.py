# -*- coding: utf-8 -*-
"""回归保护：回测口径三条不变式（2026-10-31 测评 P0-C 配套断言）。

为什么这个文件必须存在：
本轮修的三处缺陷有一个共同点 —— **改错了不会报错，只会让数字更好看**。
  · 年化因子写死 √244：20 日收益被当日收益年化，虚增 √20 = 4.47 倍；
  · sell 方向不取负：做空按做多记账，看空信号显示和看多一样赚钱；
  · 成交价用信号当日收盘：信号由当日 close 派生，却用该 close 成交，属前视。
这类缺陷靠人眼审不出来（它长得像正常数字），只能靠"能跑的断言"盯着。
所以下面每一条都是**可机判**的，不写"注意口径"这种话。
"""
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))

from viewer import signal_backtest as SB  # noqa: E402


# ── 1) 年化因子必须随持有期变化 ──────────────────────────────────────────────
def test_sharpe_scales_with_holding_period():
    """fwd 越大，年化因子越小；且严格等于 1/√fwd 的关系。

    判据（可直接手算）：sharpe(fwd=h) == sharpe(fwd=1) / sqrt(h)。
    若哪天有人把因子改回写死的 √244，这条会立刻红。
    """
    rets = [0.01, -0.008, 0.013, 0.004, -0.011, 0.007, 0.002, -0.005, 0.009, 0.001]
    s1 = SB._sharpe(rets, fwd=1)
    for h in (5, 10, 20):
        sh = SB._sharpe(rets, fwd=h)
        assert sh == pytest.approx(s1 / math.sqrt(h), rel=1e-12), (
            "fwd=%d 的年化因子不是 1/√fwd —— 说明又回到把 %d 日收益当日收益年化了" % (h, h))


def test_sharpe_20d_inflation_is_exactly_sqrt20():
    """复现测评里的 4.47：错误口径 / 正确口径 必须恰为 √20。

    这个数不是"大致差不多"，是恒等式 —— 所以它同时也是"两种口径不可混用"的证明：
    同一批收益、同一个 sd，两个数只差一个常数因子，而它们会被写进同一张表。
    """
    rets = [0.02, -0.015, 0.031, 0.008, -0.022, 0.014]
    correct = SB._sharpe(rets, fwd=20)
    wrong = SB._sharpe(rets, fwd=1)          # fwd=1 即旧口径（写死 √244）
    assert wrong / correct == pytest.approx(math.sqrt(20), rel=1e-12)


def test_stats_sharpe_matches_sharpe_helper():
    """_stats 的夏普必须与 _sharpe 同口径 —— 两条路算同一个量，不能各算各的。"""
    rets = [0.012, -0.006, 0.009, 0.003, -0.014, 0.007, 0.0, 0.005]
    store = {}
    for r in rets:
        SB._bump(store, "k", r, 0.001)
    b = store["k"]
    for h in (1, 5, 20):
        st = SB._stats(b, 0.001, h)
        assert st["sharpe"] == pytest.approx(SB._sharpe(rets, fwd=h), rel=1e-9)


# ── 2) 重叠样本的 t 值必须被折减 ─────────────────────────────────────────────
def test_t_adj_divides_by_sqrt_fwd_and_n_eff():
    """t_adj = t_naive / √fwd，n_eff = n / fwd。

    n 与 n_eff 差几倍，就说明"样本量 156,077"这个数不能直接当置信度用 ——
    这正是 judge 用 n<500 判"样本不足"时最容易忽略的一步。
    """
    rets = [0.01, -0.007, 0.012, 0.004, -0.009, 0.006]
    store = {}
    for r in rets:
        SB._bump(store, "k", r, 0.0)
    b = store["k"]
    for h in (5, 10, 20):
        st = SB._stats(b, 0.0, h)
        assert st["t_adj"] == pytest.approx(st["t_naive"] / math.sqrt(h), rel=1e-12)
        assert st["n_eff"] == int(st["n"] / h)


# ── 3) 方向符号 ──────────────────────────────────────────────────────────────
def test_sell_direction_negates_return():
    """sell 必须取负；buy 必须原样。这是"做空按做多记账"的唯一防线。"""
    for r in (0.03, -0.03, 0.0, 0.0123):
        assert SB._dir_ret(r, "buy") == r
        assert SB._dir_ret(r, "sell") == -r


def test_sell_winrate_is_complement_of_buy_for_same_returns():
    """同一批收益：sell 的"赚"应当对应 buy 的"亏"。

    判据用严格互补：不含 fee 时 winrate_buy + winrate_sell == 1（忽略 ret==0 的零测度情形）。
    旧口径下这个等式恒不成立，且差额恰好落在"看空信号也在赚钱"上。
    """
    rets = [0.02, -0.01, 0.03, -0.025, 0.005, -0.002]
    store_b, store_s = {}, {}
    for r in rets:
        SB._bump(store_b, "k", SB._dir_ret(r, "buy"), 0.0)
        SB._bump(store_s, "k", SB._dir_ret(r, "sell"), 0.0)
    wb = SB._stats(store_b["k"], 0.0, 1)["winrate"]
    ws = SB._stats(store_s["k"], 0.0, 1)["winrate"]
    assert wb + ws == pytest.approx(1.0, rel=1e-12)


# ── 4) 口径版本戳 ────────────────────────────────────────────────────────────
def test_calc_version_present():
    """口径变了就必须换版本戳，否则两种不可比的数字会长得一模一样地躺在同一张表里。

    v2 = 年化因子 √(244/fwd) + sell 取负 + T+1 **收盘价**成交
    v3 = 在 v2 基础上把成交价改为 T+1 **开盘价**，并剔除一字板不可成交样本
    两者的收益数字不可比，所以戳必须跟着变；这条断言就是防止有人只改代码不改戳。
    """
    assert isinstance(SB.CALC_VERSION, str) and SB.CALC_VERSION.strip()
    assert SB.CALC_VERSION.startswith("v3"), (
        "本轮把成交价从 T+1 收盘改为 T+1 开盘并加了不可成交剔除，版本戳不该还停在 v2")
    assert "t1open" in SB.CALC_VERSION, (
        "版本戳必须自己说明是哪种成交价口径，否则读表的人要靠猜")
