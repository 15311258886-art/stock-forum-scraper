# -*- coding: utf-8 -*-
"""决策④：持有期三档（2026-10-31）。

要钉的两件事都不是"功能有没有"，而是"会不会悄悄说错话"：

  1. **中位数的取法**。原来 `xs[len(xs)//2]` 在偶数个元素时取上侧，
     [40,140] 报成 140 —— 不报错、看起来合理，只是把成本估贵 55%；
  2. **持有期不许影响容量**。这条是业务语义：容量约束是"单票下单额／当日成交额"，
     与拿多久无关。如果哪天有人"顺手"把 hold_days 乘进容量公式，
     界面就会出现"拿久一点能多装钱"这种听上去很顺、实际完全假的结论。
"""
import os

import pytest

from viewer import executability as X
from viewer.kernel import params as P

_KLINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "kline_daily.db")
HAS_KLINE = os.path.exists(_KLINE)
need_kline = pytest.mark.skipif(not HAS_KLINE, reason="kline_daily.db 不存在")


# ── 中位数 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("xs,want", [
    ([], None),
    ([7], 7.0),
    ([40, 140], 90.0),          # 上中位数会给出 140 —— 这是本条测试的全部意义
    ([10, 20, 90], 20.0),
    ([1, 2, 3, 4], 2.5),
    ([None, 5, 15], 10.0),
])
def test_median(xs, want):
    assert X._median(xs) == want


def test_median_is_not_upper_median():
    """显式区分：务必不是 xs[len//2]。"""
    xs = [40, 140]
    assert X._median(xs) == 90.0
    assert xs[len(xs) // 2] == 140          # 旧写法给的值


# ── 年化成本 ──────────────────────────────────────────────────────────────
def test_annual_cost_formula():
    # 10 日持有、单边 10bp 佣金 + 10bp 冲击 → 往返 40bp，年轮换 24.4
    got = X.annual_cost_bp(10, 10, 10)
    assert abs(got - (P.TRADING_DAYS_PER_YEAR / 10.0 * 40)) < 1e-6


def test_annual_cost_scales_inversely_with_hold_days():
    a3, a10, a20 = (X.annual_cost_bp(h, 10, 10) for h in (3, 10, 20))
    assert a3 > a10 > a20
    # 3 日 vs 20 日 = 6.67 倍，这是"短线打法真正的门槛"
    assert abs(a3 / a20 - 20.0 / 3.0) < 1e-9


def test_annual_cost_unknown_is_none_not_zero():
    """ADV 不可估计时成本算不出来 —— 必须 None，不能写 0（写 0 就是"免费交易"）。"""
    assert X.annual_cost_bp(10, 10, None) is None
    assert X.annual_cost_bp(None, 10, 10) is None
    assert X.annual_cost_bp(0, 10, 10) is None


# ── 三档 ──────────────────────────────────────────────────────────────────
@need_kline
def test_sweep_returns_three_scenarios():
    r = X.sweep_hold_days(["000001"], aum=1_000_000, n_hold=1, fee_bp=10)
    assert r["ok"] is True
    assert [s["hold_days"] for s in r["scenarios"]] == [3, 10, 20]
    assert all(s["ok"] for s in r["scenarios"])


@need_kline
def test_hold_days_never_changes_capacity():
    """容量与持有期无关 —— 三档必须给出同一个上界（逐位相同，不是近似）。"""
    r = X.sweep_hold_days(["000001", "601518"], aum=2_000_000, n_hold=2, fee_bp=10)
    caps = {s["capacity_upper"] for s in r["scenarios"]}
    assert len(caps) == 1, "持有期不该影响容量：%s" % caps


@need_kline
def test_hold_days_never_changes_verdict():
    r = X.sweep_hold_days(["000001"], aum=1_000_000, n_hold=1, fee_bp=10)
    assert len({s["verdict"] for s in r["scenarios"]}) == 1


@need_kline
def test_sweep_cost_decreases_with_longer_hold():
    r = X.sweep_hold_days(["000001"], aum=1_000_000, n_hold=1, fee_bp=10)
    ann = [s["annual_cost_bp_median"] for s in r["scenarios"]]
    assert ann[0] > ann[1] > ann[2]


@need_kline
def test_sweep_reports_unpriced_count():
    """算不出成本的票要计入 n_unpriced，不能被 median 静默吃掉。"""
    d = X.evaluate(["000001"], 1_000_000, 1, 10, fee_bp=10)
    cs = d["cost_summary"]
    assert cs["n_priced"] + cs["n_unpriced"] == len(d["per_code"])
    assert "不是'成本为 0'" in cs["note"]


def test_missing_field_still_rejected_in_sweep():
    """三档不能成为绕过必填校验的后门。"""
    r = X.sweep_hold_days(["000001"], aum=1_000_000, n_hold=1, fee_bp=-1)
    assert r["scenarios"][0]["ok"] is False
    assert r["scenarios"][0]["error"] == "missing_required"
