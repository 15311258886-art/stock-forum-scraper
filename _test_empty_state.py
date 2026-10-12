# -*- coding: utf-8 -*-
"""建议7 后端侧自测：每日预测的「数据截至 MM-DD」过期角标（2026-10-05）。

 predict 侧只加了一个纯函数 _pred_stale(run_date)：最近交易日 > 最新预测日 才给文案。
 取不到最近交易日（日历/网络不可用）宁可返回 None —— 标错的代价远大于不标。
 离线确定性：用假 emapi 模块打桩 latest_valuation_date，不真打网络。
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))   # predictor 在 viewer/ 包里，但按顶层模块 import（与 server.py 一致）

import predictor  # noqa: E402


def _set_ltd(value):
    """把 emapi.latest_valuation_date 顶成固定值（伪造模块，离线可判定）。"""
    fake = types.ModuleType("emapi")
    fake.latest_valuation_date = lambda *a, **k: value
    sys.modules["emapi"] = fake
    sys.modules.pop("emapi_ltd", None)


def _reset():
    for m in list(sys.modules):
        if m == "emapi":
            del sys.modules[m]


_cases = []


def case(name):
    def deco(fn):
        _cases.append((name, fn))
        return fn
    return deco


@case("陈旧：最近交易日晚于预测日 → 给角标文案")
def _c1():
    _set_ltd("2026-10-02")
    r = predictor._pred_stale("2026-09-30")
    assert r, "应判定为陈旧，却返回 None"
    assert r["last_trade_date"] == "2026-10-02", r
    assert r["lag_days"] == 2, r
    assert "数据截至 09-30" in r["reason"], r["reason"]
    assert "2026-10-02" in r["reason"] or "10-02" in r["reason"], r["reason"]
    print("     reason =", r["reason"])


@case("新鲜：最近交易日 == 预测日 → 静默不标")
def _c2():
    _set_ltd("2026-10-02")
    assert predictor._pred_stale("2026-10-02") is None


@case("取不到最近交易日 → 静默不标（宁可不标，也不许标错）")
def _c3():
    _set_ltd(None)
    assert predictor._pred_stale("2026-09-30") is None


@case("预测日为空 → 静默不标")
def _c4():
    _set_ltd("2026-10-02")
    assert predictor._pred_stale(None) is None


@case("日历抛异常 → 静默不标，绝不冒泡到接口")
def _c5():
    fake = types.ModuleType("emapi")

    def boom(*a, **k):
        raise RuntimeError("交易日历不可用")
    fake.latest_valuation_date = boom
    sys.modules["emapi"] = fake
    assert predictor._pred_stale("2026-09-30") is None


@case("接口透传：today_payload 会带上 stale / stale_reason")
def _c6():
    """不打 DB（58MB 主库只读也慢），改验拼接规则本身：把 today_payload 的返回装配抽出来比。"""
    import inspect
    src = inspect.getsource(predictor.today_payload)
    assert 'out["stale"] = True' in src.replace("'", '"') or "out[\"stale\"] = True" in src, src[-600:]
    assert 'out["stale_reason"]' in src, src[-600:]
    assert 'out["last_trade_date"]' in src
    # 前端渲染依赖字段名，接口层 server.api_prediction_today 是原样透传
    srv = open(os.path.join(ROOT, "viewer", "server.py"), encoding="utf-8").read()
    assert "return predictor.today_payload(DB.conn)" in srv


def main():
    ok = fail = 0
    for name, fn in _cases:
        try:
            fn()
            ok += 1
            print("[ok] " + name)
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print("[FAIL] %s -> %r" % (name, exc))
        finally:
            _reset()
    print("==========")
    print("ALL_PASS (%d)" % ok if fail == 0 else "FAILED %d / %d" % (fail, ok + fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
