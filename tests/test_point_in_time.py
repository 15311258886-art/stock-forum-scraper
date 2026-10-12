# -*- coding: utf-8 -*-
"""A3-1 时点纪律（防"时间穿越"）测试 —— 规格书验收要求：
「存在 1 条以上『喂未来数据被拒绝』的失败单测且通过」。

predictor.py 在 import 期会建库连接，故用 ast 抽取目标函数单独执行（被测的是线上源码）。
"""
from __future__ import annotations

import ast
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREDICTOR_PY = os.path.join(ROOT, "viewer", "predictor.py")


def _guard():
    """从 predictor.py 源码抽出 LookaheadError + assert_point_in_time（避开 import 副作用）。"""
    ns: dict = {}
    src = io.open(PREDICTOR_PY, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "LookaheadError":
            exec(ast.get_source_segment(src, node), ns)
        if isinstance(node, ast.FunctionDef) and node.name == "assert_point_in_time":
            exec(ast.get_source_segment(src, node), ns)
    return ns["LookaheadError"], ns["assert_point_in_time"]


def test_guard_exists():
    src = io.open(PREDICTOR_PY, encoding="utf-8").read()
    assert "class LookaheadError" in src, "predictor.py 缺少 LookaheadError"
    assert "def assert_point_in_time" in src, "predictor.py 缺少 assert_point_in_time"
    return True


def test_future_data_is_rejected():
    """核心断言：喂进晚于预测日的特征日期 → 必须被拒绝。"""
    LookaheadError, guard = _guard()
    raised = False
    try:
        guard("2026-10-09", "2026-10-01", "2026-10-13")   # 最后一个是未来
    except LookaheadError as e:
        raised = True
        assert "前视偏差" in str(e)
    assert raised, "喂未来数据（2026-10-13 > 2026-10-09）竟未被拒绝！防前视失效"
    return True


def test_past_and_equal_dates_pass():
    LookaheadError, guard = _guard()
    guard("2026-10-09", "2026-09-01", "2026-10-09", None, "")   # 同日/更早/空值 均放行
    return True


def test_none_asof_disables_check():
    """asof 为空 → 不设边界（全量重建场景），不得误伤。"""
    LookaheadError, guard = _guard()
    guard(None, "2099-01-01")
    return True


def test_feature_entry_is_guarded():
    """防止门禁被绕过：生成路径在 K 线截断后必须真的调用守卫。"""
    src = io.open(PREDICTOR_PY, encoding="utf-8").read()
    i = src.find("截断到 %s 后候选池 K 线为空")
    assert i > 0, "找不到截断点"
    seg = src[i:i + 700]
    assert "assert_point_in_time(" in seg, "K 线截断后未接时点守卫（A3-1 形同虚设）"
    return True


if __name__ == "__main__":
    import sys

    tests = [test_guard_exists, test_future_data_is_rejected,
             test_past_and_equal_dates_pass, test_none_asof_disables_check,
             test_feature_entry_is_guarded]
    fails = []
    for t in tests:
        try:
            ok = t()
            print(("PASS " if ok else "FAIL ") + t.__name__)
            if not ok:
                fails.append(t.__name__)
        except AssertionError as e:
            print("FAIL " + t.__name__ + " :: " + str(e))
            fails.append(t.__name__)
        except Exception as e:  # noqa: BLE001
            print("ERROR " + t.__name__ + " :: " + repr(e))
            fails.append(t.__name__ + "(error)")
    print("\n==== %d/%d PASS ====" % (len(tests) - len(fails), len(tests)))
    sys.exit(1 if fails else 0)
