"""成本口径表（决策①）的守卫测试。

这一组断言的目的很具体：**防止两套口径日后被"统一"掉**。
统一看起来是"简化"，实际是让一个数去回答它答不了的问题 ——
回测要固定、保守、可比；执行要随流动性变化。合并当天就会有人拿
25bp 去核 15bp，然后判定系统算错了。
"""
from viewer.kernel import cost_basis as CB
from viewer.kernel import params as P


def test_two_bases_exist_and_keyed():
    keys = [b["key"] for b in CB.COST_BASIS]
    assert keys == ["backtest", "execution"], "口径的 key 或顺序变了，前端按 key 渲染"
    assert CB.describe()["count"] == 2


def test_backtest_fee_is_single_source():
    """回测口径的数值必须来自 params，不能在 cost_basis 里另写一个。"""
    b = [x for x in CB.COST_BASIS if x["key"] == "backtest"][0]
    assert b["fixed"] is True
    assert abs(b["roundtrip_bp"] - P.BACKTEST_FEE_ROUNDTRIP * 10000.0) < 1e-6
    assert abs(b["roundtrip_bp"] - 15.0) < 1e-6, "15bp 是当前口径，变了要同时改注释与文档"


def test_execution_tiers_come_from_params():
    e = [x for x in CB.COST_BASIS if x["key"] == "execution"][0]
    assert e["fixed"] is False
    assert e["roundtrip_bp"] is None, "执行口径随 ADV 变，不该有一个单一数值"
    assert [(int(a), float(b)) for a, b in e["tiers"]] == [(int(a), float(b)) for a, b in P.COST_TIERS_BP]


def test_tax_asymmetry_is_explicit():
    """印花税只在一边：回测含、执行不含。这个不对称必须写在字段里，不能靠注释。"""
    b = [x for x in CB.COST_BASIS if x["key"] == "backtest"][0]
    e = [x for x in CB.COST_BASIS if x["key"] == "execution"][0]
    assert b["flag"] == "含印花税"
    assert e["flag"] == "不含印花税"
    assert any("印花税" in s for s in b["includes"])
    assert any("印花税" in s for s in e["excludes"])


def test_reconcile_forbids_naive_subtraction():
    """必须留下"别拿两个数相减"这条，否则最容易被误用的正是这个减法。"""
    r = CB.RECONCILE
    assert r["same_question"] is False
    assert "不要" in r["dont"]
    assert "更差" in r["direction_matters"]


def test_describe_is_json_safe_and_pure():
    """不读库、不计算 —— 两次调用结果必须逐位相同。"""
    a = CB.describe()
    b = CB.describe()
    assert a == b
    assert a["ok"] is True
    assert "tiers" in [x for x in a["basis"] if x["key"] == "execution"][0]
