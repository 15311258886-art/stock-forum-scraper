# -*- coding: utf-8 -*-
"""执行可行性 / 量纲 / 多重比较的判据测试（2026-10-31 新增，对应测评 P1）。

这些测试钉的是**口径**，不是数值：
  · BH 校正必须单调，且小 p 不会被校正推高到比大 p 更高的 q；
  · 量纲自证必须在"手""股""判不出"三种情况下分别给出正确答案，
    尤其是**判不出时不许回退默认倍数**；
  · 执行可行性缺字段必须报缺，不许用默认值顶替。
"""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer"))

from viewer import units as U                      # noqa: E402
from viewer.signal_backtest import _bh_adjust, _p_two_sided   # noqa: E402
from viewer.kernel import params as P              # noqa: E402


# ── BH 多重比较校正 ────────────────────────────────────────────────────────────
def test_bh_classic_example():
    """经典例：p=0.01..0.05, m=5 → 全部 q=0.05。"""
    pairs = [("a", 0.01), ("b", 0.02), ("c", 0.03), ("d", 0.04), ("e", 0.05)]
    q = _bh_adjust(pairs)
    for k in "abcde":
        assert abs(q[k] - 0.05) < 1e-12, (k, q)


def test_bh_is_monotone_in_p():
    """q 必须随 p 单调不减 —— 否则会输出「更显著者 q 更大」的自相矛盾结果。"""
    pairs = [("a", 0.001), ("b", 0.02), ("c", 0.03), ("d", 0.9)]
    q = _bh_adjust(pairs)
    ordered = [q[k] for k in ("a", "b", "c", "d")]
    assert all(ordered[i] <= ordered[i + 1] + 1e-12 for i in range(3)), ordered


def test_bh_pushes_small_p_up_not_down():
    """BH 只会把 p 抬大（更保守），绝不会把 p 压小。"""
    pairs = [("a", 0.01), ("b", 0.2)]
    q = _bh_adjust(pairs)
    for k, p in pairs:
        assert q[k] >= p - 1e-12, (k, q[k], p)


def test_bh_ignores_none():
    """p 为 None 的项不进分母：它没被检验过，不该稀释 q。"""
    q1 = _bh_adjust([("a", 0.01), ("b", 0.04)])
    q2 = _bh_adjust([("a", 0.01), ("b", 0.04), ("c", None)])
    assert abs(q1["a"] - q2["a"]) < 1e-12
    assert q2["c"] is None


def test_p_two_sided_matches_normal_quantile():
    assert abs(_p_two_sided(1.959963985) - 0.05) < 1e-6
    assert abs(_p_two_sided(0.0) - 1.0) < 1e-12
    assert _p_two_sided(None) is None


# ── 量纲自证 ──────────────────────────────────────────────────────────────────
def test_volume_unit_hand():
    """000001 于 2026-10-27：volume=1,323,113，amount=14.86 亿，close=11.30 → 手。"""
    assert U.infer_volume_unit(1323113.0, 1486000000.0, 11.30) == "hand"


def test_volume_unit_share():
    """同一笔成交若按"股"记录，应是 132,311,300 量级 → 判为 share。"""
    assert U.infer_volume_unit(132311300.0, 1486000000.0, 11.30) == "share"


def test_volume_unit_unknown_does_not_guess():
    """判不出时必须返回 None，**不许**回退到默认倍数。"""
    assert U.infer_volume_unit(1000.0, 1486000000.0, 11.30) is None   # 比值 13 万倍
    assert U.volume_to_shares(1000.0, 1486000000.0, 11.30) is None


def test_volume_to_shares_multiplies_hand_by_100():
    assert U.volume_to_shares(1000.0, 1000.0 * 10.0 * 100, 10.0) == 100000.0
    assert U.volume_to_shares(1000.0, 1000.0 * 10.0, 10.0) == 1000.0


def test_convertible_bond_detected():
    """111024 / 113710 实测混在股票快照表里，必须被认出来。"""
    assert U.is_convertible_bond("111024")
    assert U.is_convertible_bond("113710")
    assert not U.is_convertible_bond("600519")


# ── 涨跌停幅度 ────────────────────────────────────────────────────────────────
def test_price_limit_by_board():
    assert U.price_limit_pct("600519", "贵州茅台") == 0.10
    assert U.price_limit_pct("300750", "宁德时代") == 0.20
    assert U.price_limit_pct("688981", "中芯国际") == 0.20
    assert U.price_limit_pct("000001", "*ST某某") == 0.05
    assert U.price_limit_pct("999999", "") is None


def test_limit_price_rounding():
    assert U.limit_up_price(11.30, "000001", "平安银行") == 12.43   # 12.43
    assert U.limit_down_price(11.30, "000001", "平安银行") == 10.17


# ── 执行可行性 ────────────────────────────────────────────────────────────────
def test_exec_requires_all_four_fields():
    from viewer import executability as E
    r = E.evaluate(["000001"], aum=None, n_hold=1, hold_days=1, fee_bp=1)
    assert r["ok"] is False and r["error"] == "missing_required"
    assert set(r["missing"]) == {"aum"}
    r2 = E.evaluate(["000001"], aum=1e6, n_hold=0, hold_days=-1, fee_bp=-1)
    assert set(r2["missing"]) == {"n_hold", "hold_days", "fee_bp"}


def test_capacity_formula_is_point_estimate_with_zero_lower_bound():
    """容量上界必须是 CAPACITY_PARTICIPATION × N × min(ADV)，下界显式给 0。"""
    cap = P.aum_cap_point(20, 100_000_000)
    assert abs(cap - 0.001444 * 20 * 100_000_000) < 1e-6
    assert P.aum_cap_point(0, 1e8) is None
    assert P.aum_cap_point(20, None) is None


def test_cost_tier_none_vs_big_must_differ():
    """ADV 不可估计（None）与"很贵"必须分开，不能都返回一个大数。"""
    assert P.cost_bp_of_adv(None) is None
    assert P.cost_bp_of_adv(1e5) is None          # 低于最低档 → 不入档，不是"最贵档"
    assert P.cost_bp_of_adv(6e8) == 10.0
    assert P.cost_bp_of_adv(2.5e7) == 60.0


def test_param_invariant_gap_is_exposed():
    """40 vs 500 的矛盾必须仍被算出来并对外可见（不因整改而静默消失）。"""
    g = P.invariant_gap()
    assert g["judge_min_n"] == P.JUDGE_MIN_N
    assert g["band"] > 0 and g["consistent"] is False


def test_eff_matches_registry():
    assert abs(P.eff_of(P.EFF_SCALE) - (1 - math.exp(-1))) < 1e-12


# ── 迁移纪律：加列失败必须当场炸，不能拖到写回时洗掉全部计算 ──────────────
def test_schema_check_raises_on_missing_column():
    """`_assert_schema` 必须在缺列时抛错。

    事故背景（2026-10-31）：`_ensure_col` 里 `except: pass` 把 ALTER 的失败吞了，
    5,337 只票算完十几分钟，最后一句 INSERT 才报
    "table signal_backtest has no column named n_codes" —— 全部作废。
    """
    import sqlite3
    from viewer.signal_backtest import _assert_schema
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (a INTEGER)")
    _assert_schema(con, "t", ("a",))          # 齐了不抛
    try:
        _assert_schema(con, "t", ("a", "n_codes"))
    except RuntimeError as e:
        assert "n_codes" in str(e)
    else:
        raise AssertionError("缺列时没有抛错 —— 迁移失败又变成静默的了")


def test_ensure_col_refuses_silently_missing_table():
    """表不存在时 `_ensure_col` 必须报错（不再 return/pass）。"""
    import sqlite3
    from viewer.signal_backtest import _ensure_col
    con = sqlite3.connect(":memory:")
    try:
        _ensure_col(con, "no_such_table", "x", "INTEGER")
    except RuntimeError:
        pass
    else:
        raise AssertionError("对不存在的表加列竟然没报错")


def test_insert_columns_are_declared():
    """写回字段清单与 INSERT 语句必须对得上（单一来源的落地检查）。"""
    import io
    import os
    import re
    from viewer.signal_backtest import _BT_COLS
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "viewer", "signal_backtest.py"),
        encoding="utf-8", errors="ignore").read()
    m = re.search(r"INSERT OR REPLACE INTO signal_backtest\"(.*?)VALUES", src, re.S)
    assert m, "没找到 signal_backtest 的 INSERT 字段列表"
    # INSERT 是若干段字符串字面量拼起来的：去掉引号与空白后按逗号切
    blob = re.sub(r"\s+", "", m.group(1).replace('"', ""))
    blob = blob.strip().strip("()")
    cols = [c for c in blob.split(",") if c]
    assert tuple(cols) == _BT_COLS, "INSERT 字段与 _BT_COLS 不一致：%s vs %s" % (cols, list(_BT_COLS))
