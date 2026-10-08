# -*- coding: utf-8 -*-
"""reasonableness_gate 单元测试：越界触发、边界不触发、负数 drawdown 等。

运行：
  .venv/Scripts/python.exe test_reasonableness_gate.py
  .venv/Scripts/python.exe -m pytest test_reasonableness_gate.py -q

设计契约（与 viewer/reasonableness_gate.py 一致）：
- 指标在合理区间内 → GateResult.ok=True，且任何模式都不抛。
- 指标越界：
    * test_mode=False（生产/非测试）→ 返回 error 级 GateResult（ok=False），绝不抛、绝不阻断。
    * test_mode=True（测试/CI）→ 抛 GateViolation，使测试/CI 失败。
- 指标为 None → skip（level="skip", ok=True），绝不假 PASS。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "viewer"))

from reasonableness_gate import (  # noqa: E402
    GateViolation, check_total_return, check_sharpe, check_drawdown,
    check_daily_return, run_gates,
    TOTAL_RETURN_BOUNDS, SHARPE_BOUNDS, MAX_DRAWDOWN_BOUNDS, DAILY_RETURN_BOUNDS,
)


def test_bounds_constants_in_q6_range():
    assert TOTAL_RETURN_BOUNDS == (-0.95, 5.0)
    assert SHARPE_BOUNDS == (-3.0, 5.0)
    assert MAX_DRAWDOWN_BOUNDS == (-1.0, 0.0)
    assert DAILY_RETURN_BOUNDS == (-0.11, 0.11)


def test_total_return_out_of_bounds():
    """越界：生产模式返回 error 结果（ok=False）；测试模式抛 GateViolation。"""
    # 非测试模式：返回 error 结果，不抛
    assert not check_total_return(9.0, {"test_mode": False}).ok
    assert not check_total_return(-2.0, {"test_mode": False}).ok
    # 测试模式：抛 GateViolation（CI 据此失败）
    raised = False
    try:
        check_total_return(9.0, {"test_mode": True})
    except GateViolation:
        raised = True
    assert raised, "test_mode 下越界应抛 GateViolation"


def test_total_return_in_bounds_ok():
    """区间内：ok=True，测试模式不抛。"""
    ctx = {"test_mode": True}
    assert check_total_return(0.12, ctx).ok
    assert check_total_return(-0.5, ctx).ok
    assert check_total_return(5.0, ctx).ok    # 边界上限包含
    assert check_total_return(-0.95, ctx).ok  # 边界下限包含


def test_sharpe_out_of_bounds():
    """越界：生产模式返回 error；区间内：ok=True。"""
    assert not check_sharpe(7.0, {"test_mode": False}).ok
    assert not check_sharpe(-5.0, {"test_mode": False}).ok
    assert check_sharpe(0.0, {"test_mode": True}).ok
    assert check_sharpe(5.0, {"test_mode": True}).ok


def test_drawdown_negative_is_ok():
    """负数 drawdown（亏损幅度，落在 [-1,0] 内）→ ok，不误报。"""
    ctx = {"test_mode": True}
    assert check_drawdown(-0.07, ctx).ok
    assert check_drawdown(-1.0, ctx).ok   # 边界
    assert check_drawdown(0.0, ctx).ok     # 无回撤


def test_drawdown_positive_or_too_deep_is_error():
    """正数 drawdown（误把收益标为回撤）/ 超 -1 → error；测试模式抛。"""
    assert not check_drawdown(0.3, {"test_mode": False}).ok
    assert not check_drawdown(-1.5, {"test_mode": False}).ok
    raised = False
    try:
        check_drawdown(0.3, {"test_mode": True})
    except GateViolation:
        raised = True
    assert raised, "test_mode 下正数 drawdown 应抛 GateViolation"


def test_daily_return_bounds():
    """单日收益超 ±11% → error；区间内 → ok。"""
    assert check_daily_return(0.05, {"test_mode": True}).ok
    assert not check_daily_return(0.2, {"test_mode": False}).ok    # 超过 ±11%
    assert not check_daily_return(-0.2, {"test_mode": False}).ok
    assert check_daily_return([0.01, -0.02, 0.0], {"test_mode": True}).ok
    r = check_daily_return([0.01, 0.5, -0.02], {"test_mode": False})
    assert (not r.ok) and "1" in r.message


def test_none_metric_skips():
    """缺指标（None）→ skip，绝不假 PASS。"""
    ctx = {"test_mode": True}
    for fn in (check_total_return, check_sharpe, check_drawdown):
        r = fn(None, ctx)
        assert r.ok and r.level == "skip"


def test_run_gates_aggregate_and_production_no_raise():
    """生产模式（test_mode=False）：越界不应抛，应返回 error 级结果（并写告警，不阻断）。"""
    metrics = {"total_return": 9.0, "sharpe": 0.9, "max_drawdown": -0.2,
               "daily_returns": [0.01]}
    results = run_gates(metrics, {"test_mode": False, "module": "t", "function": "t"})
    names = {r.name: r for r in results}
    assert not names["total_return"].ok
    # 生产模式不抛异常
    assert True


if __name__ == "__main__":
    for name in list(globals()):
        if name.startswith("test_") and callable(globals()[name]):
            globals()[name]()
            print("PASS", name)
    print("ALL GATE TESTS PASSED")
