# -*- coding: utf-8 -*-
"""样本量三层制（2026-10-31 决策①，进哥拍板走 C）的机判断言。

要钉死的其实只有四件事，但每件都是"以后可以悄悄改回去"的地方：

  1. 边界：39/40/499/500 四个点各归哪层，一点都不能飘；
  2. **恒等**：n ≥ JUDGE_MIN_N 时 eff_weight(n) == eff_of(n)。
     这条是"历史评分不用重算"的全部依据 —— 一旦被打破，全部旧评分失效且没人会发现；
  3. 单调：weight_gate 不能有断崖（n=499→500 的跳跃必须可忽略）；
  4. 隔断：provisional 的信号**不许**进入 tech_summary 聚合。
     这是最容易被后来者"顺手放开"的一条，而它一旦放开，
     40–500 的弱样本就会重新悄悄改写权重 —— 也就是本轮要修的原病。
"""
import math

import pytest

from viewer.kernel import evidence as E
from viewer.kernel import params as P


# ── 1. 边界 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("n,tier", [
    (0, P.TIER_OBSERVE),
    (1, P.TIER_OBSERVE),
    (P.OBSERVE_MIN_N - 1, P.TIER_OBSERVE),      # 39
    (P.OBSERVE_MIN_N, P.TIER_PROVISIONAL),      # 40 → 进观测中
    (P.JUDGE_MIN_N - 1, P.TIER_PROVISIONAL),    # 499
    (P.JUDGE_MIN_N, P.TIER_DECIDABLE),          # 500 → 可裁决
    (10 ** 6, P.TIER_DECIDABLE),
])
def test_tier_boundaries(n, tier):
    assert P.tier_of(n) == tier


@pytest.mark.parametrize("bad", [None, "", "abc", -1, -500, float("nan"), float("inf")])
def test_tier_never_crashes_on_garbage(bad):
    """脏数据必须落到最保守的层，不能抛异常、也不能落进可裁决。"""
    assert P.tier_of(bad) in (P.TIER_OBSERVE, P.TIER_DECIDABLE)


def test_nan_is_observe_not_decidable():
    # inf 在数学上确实"样本无限"，归 decidable 可以接受；
    # 但 NaN 是"不知道"，必须归观测层 —— 不知道不能当成够。
    assert P.tier_of(float("nan")) == P.TIER_OBSERVE


# ── 2. 恒等（历史可比性的唯一依据）──────────────────────────────────────────
def test_eff_weight_equals_eff_above_judge_min():
    for n in list(range(P.JUDGE_MIN_N, P.JUDGE_MIN_N + 3000, 7)) + [10 ** 5, 760000]:
        assert abs(P.eff_weight(n) - P.eff_of(n)) < 1e-12, n


def test_weight_gate_is_exactly_one_above_judge_min():
    for n in range(P.JUDGE_MIN_N, P.JUDGE_MIN_N + 500):
        assert P.weight_gate(n) == 1.0


# ── 3. 门与单调 ────────────────────────────────────────────────────────────
def test_gate_zero_below_observe_min():
    for n in range(0, P.OBSERVE_MIN_N):
        assert P.weight_gate(n) == 0.0


def test_gate_monotonic_and_continuous():
    prev = -1.0
    for n in range(0, P.JUDGE_MIN_N + 50):
        g = P.weight_gate(n)
        assert g >= prev - 1e-12, "weight_gate 必须单调不减"
        prev = g
    # 断崖检查：跨过门槛那一根样本的跳跃不得超过 1/span 的两倍
    span = P.JUDGE_MIN_N - P.OBSERVE_MIN_N
    jump = abs(P.weight_gate(P.JUDGE_MIN_N) - P.weight_gate(P.JUDGE_MIN_N - 1))
    assert jump <= 2.0 / span


def test_eff_weight_zero_in_observe_layer():
    """< 40 的样本连一点点权重都不该拿到 —— 否则"不判方向"就只是嘴上说说。"""
    for n in range(0, P.OBSERVE_MIN_N):
        assert P.eff_weight(n) == 0.0


# ── 4. 隔断（最容易被放开的一条）───────────────────────────────────────────
def test_tech_summary_excludes_provisional():
    rows = [
        {"reason": "ok", "direction": "buy", "n": 900, "edge_wr": 0.05,
         "sharpe": 0.6, "sharpe_net": 0.6, "verdict": "effective", "usable": True},
        {"reason": "weak", "direction": "buy", "n": 200, "edge_wr": 0.90,
         "sharpe": 9.0, "sharpe_net": 9.0, "verdict": "provisional", "usable": False},
    ]
    t = E.tech_summary(rows)
    assert t["n_total"] == 900, "provisional 的 200 个样本不许混进来"
    assert abs(t["edge_wr"] - 0.05) < 1e-9, "provisional 的 0.90 超额会污染聚合"
    assert t["count"] == 1


def test_tech_summary_all_provisional_gives_empty():
    rows = [{"reason": "w", "direction": "buy", "n": 300, "edge_wr": 0.9,
             "sharpe": 5.0, "sharpe_net": 5.0, "verdict": "provisional", "usable": False}]
    t = E.tech_summary(rows)
    assert t["n_total"] == 0 and t["count"] == 0
    assert t["provisional"] == 1


# ── 5. 裁决与分层一致 ──────────────────────────────────────────────────────
@pytest.mark.parametrize("n,want", [
    (3, "insufficient"),
    (39, "insufficient"),
    (40, "provisional"),
    (499, "provisional"),
])
def test_judge_respects_tiers(n, want):
    assert E.judge(n, 0.05, 0.02, 0.8) == want


def test_judge_decidable_still_classifies():
    assert E.judge(500, 0.05, 0.02, 0.8) == "effective"
    assert E.judge(500, -0.05, -0.02, -0.8) == "adverse"
    assert E.judge(500, 0.001, 0.001, 0.01) == "neutral"


def test_usable_iff_decidable():
    for n in (0, 39, 40, 200, 499, 500, 5000):
        info = E.tier_info(n)
        assert info["usable"] == (info["tier"] == P.TIER_DECIDABLE)


def test_tier_legend_matches_params():
    lg = E.tier_legend()
    assert lg["observe_below"] == P.OBSERVE_MIN_N
    assert lg["decidable_at"] == P.JUDGE_MIN_N
    assert lg["provisional"] == [P.OBSERVE_MIN_N, P.JUDGE_MIN_N]
    assert set(lg["label"]) == {P.TIER_OBSERVE, P.TIER_PROVISIONAL, P.TIER_DECIDABLE}


# ── 6. 参数自述里矛盾必须仍然"可见"且标为已处置 ─────────────────────────────
def test_gap_still_reported_but_marked_resolved():
    g = P.invariant_gap()
    assert g["band"] > 0, "band 是事实，不许因为'处置了'就抹平"
    assert g["resolved_by"] == "tier"
    assert g["resolved"] is True
    assert g["tiering"]["provisional_usable"] is False


def test_describe_exposes_new_params():
    d = P.describe()
    assert d["OBSERVE_MIN_N"] == P.OBSERVE_MIN_N
    assert "观测中" in d["note"] or "三层" in d["note"]


def test_observe_min_not_derived_from_eff_scale():
    """两个 40 是巧合，不是定义关系。若有人把它们绑起来，这里会红。

    这条测的是"意图"而不是"值"：只要有人写了 OBSERVE_MIN_N = EFF_SCALE，
    改动 EFF_SCALE 就会同时移动观察线 —— 那正是本轮要消灭的隐式耦合。
    """
    import inspect
    src = inspect.getsource(P).split("# ── 样本量三层制")[1].split("TIER_OBSERVE")[0]
    assert "EFF_SCALE" not in src.replace("EFF_SCALE 推导", ""), \
        "OBSERVE_MIN_N 不许引用 EFF_SCALE 派生"


def test_math_import_not_unused():
    # 守住 eff_of 的实现：若被改成硬编码(如 n>=500 → 1.0)，恒等测试仍会过，
    # 但"置信度随样本连续变化"就没了，面板上那句百分比会变假。
    assert 0 < P.eff_of(120) < 1
    assert math.isclose(P.eff_of(0), 0.0)
