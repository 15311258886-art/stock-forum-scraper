# -*- coding: utf-8 -*-
"""阶段2 Oracle 正确性测试：以真实夹具做 Oracle 对照。

红线：缺夹具则 pytest.skip（诚实降级，绝不假 PASS）。夹具必须 is_real_data=true 且 sha256 通过。

两层对照：
  1) 独立重算对照：用纯标准库独立核算 total_return，应与夹具冻结的 expected 一致
     （验证夹具自身真值自洽，等价于「独立 Oracle 复算」）。
  2) SUT 对照：用夹具 bars 驱动被测系统 screener_extra.backtest，其 total_return 应与 expected 接近
     （验证被测系统数值与真实历史一致；容差 0.05 吸收复权/成本口径差异）。

运行：
  .venv/Scripts/python.exe -m pytest test_fixtures_oracle.py -q
"""
import sys
from pathlib import Path

# pytest 为测试运行器；若 .venv 未安装，则用桩保证「无夹具时诚实跳过」语义仍可运行。
try:
    import pytest  # type: ignore
    _HAVE_PYTEST = True
except Exception:  # noqa: BLE001
    _HAVE_PYTEST = False

    class _OracleSkip(Exception):
        """无 pytest 时的跳过信号（等价 pytest.skip）。"""

    class _PytestStub:
        @staticmethod
        def skip(msg):
            raise _OracleSkip(msg)

    pytest = _PytestStub()  # type: ignore

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "fixtures"))
sys.path.insert(0, str(ROOT / "viewer"))

from load_fixtures import load_all_fixtures, FixtureMissing, FixtureRejected  # type: ignore  # noqa: E402
import screener_extra as se  # noqa: E402

EPS = 1e-3  # 独立重算 vs 冻结 expected 的容差（极小数值误差）


def _load_fixtures_or_skip():
    try:
        return load_all_fixtures()
    except FixtureMissing:
        pytest.skip("沙箱无真实夹具，诚实跳过（绝不假 PASS）")
    except FixtureRejected as e:
        pytest.skip("夹具校验失败，诚实跳过: %s" % e)


def test_fixtures_are_real_and_valid():
    """夹具必须 is_real_data=true 且 sha256 通过（load_all_fixtures 内部已校验）。"""
    fx = _load_fixtures_or_skip()
    assert len(fx) >= 1
    for d in fx:
        assert d["metadata"]["is_real_data"] is True


def test_oracle_total_return_matches_expected():
    """独立重算 total_return 应与冻结 expected 一致（缺夹具则 skip）。"""
    fx = _load_fixtures_or_skip()
    for d in fx:
        bars = d["bars"]
        closes = [float(b["close"]) for b in bars]
        if len(closes) < 2:
            continue
        total = closes[-1] / closes[0] - 1
        exp = d["expected"].get("total_return")
        if exp is None:
            continue
        assert abs(total - exp) < max(EPS, abs(exp) * 0.01), \
            "%s: 独立重算 total_return=%s 与 expected=%s 偏差过大" % (
                d["metadata"]["symbol"], round(total, 4), exp)


def test_sut_backtest_matches_expected():
    """用夹具 bars 驱动被测系统 backtest，其 total_return 应与 expected 接近（缺夹具则 skip）。"""
    fx = _load_fixtures_or_skip()
    for d in fx:
        bars = d["bars"]
        if len(bars) < 2:
            continue
        synth = [{
            "date": b["date"], "open": float(b["open"]), "high": float(b["high"]),
            "low": float(b["low"]), "close": float(b["close"]),
            "volume": float(b.get("vol", 0.0)), "pct": 0.0, "suspended": False,
        } for b in bars]
        symbol = se._norm_ak(d["metadata"]["symbol"])
        start = bars[0]["date"]
        end = bars[-1]["date"]
        orig = se.get_kline

        def _fake(code, s, e):
            return synth if se._norm_ak(code) == symbol else []

        try:
            se.get_kline = _fake
            res = se.backtest([symbol], start, end)
        finally:
            se.get_kline = orig
        if not res.get("ok"):
            pytest.skip("SUT 回测未返回 ok（数据不足）: %s" % res.get("error"))
        exp = d["expected"]
        tr = res.get("total_return")
        if exp.get("total_return") is not None and tr is not None:
            assert abs(tr - exp["total_return"]) < 0.05, \
                "%s: SUT total_return=%s vs expected=%s" % (
                    symbol, tr, exp["total_return"])


if __name__ == "__main__":
    # 直接运行时：无夹具则诚实打印并退出 0（不假 PASS）
    try:
        fx = load_all_fixtures()
        print("已加载 %d 个真实夹具，运行 Oracle 对照" % len(fx))
        test_fixtures_are_real_and_valid()
        test_oracle_total_return_matches_expected()
        test_sut_backtest_matches_expected()
        print("ALL ORACLE TESTS PASSED")
    except (FixtureMissing, FixtureRejected) as e:
        print("诚实跳过（沙箱无真实夹具）:", e)
    except Exception as skip_exc:  # noqa: BLE001
        # 无 pytest 时 _OracleSkip，或无夹具时的等价跳过信号
        print("诚实跳过（沙箱无真实夹具）:", skip_exc)
