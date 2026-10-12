# -*- coding: utf-8 -*-
"""把配色约定门禁（规格书 A4-2）接入 pytest 统一门禁。

门禁本体在 tools/audit_color_convention.py；此处只做断言包装，使其随
`pytest -q` 一起跑，防止日后有人把涨跌反色写回去。
"""
from __future__ import annotations

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOL = os.path.join(ROOT, "tools", "audit_color_convention.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("audit_color_convention", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_color_convention_no_inverted():
    """全站红涨绿跌：语义变量与 up/down 类不得反色。"""
    mod = _load_tool()
    code = mod.main()
    assert code == 0, "配色约定门禁未通过（存在涨跌反色，详见 audit_color_convention 输出）"
    return True


def test_color_gate_can_detect_inversion():
    """反向自检：把 --bull 换成绿色时，门禁必须能判定为违规（防止门禁退化成永远通过）。"""
    mod = _load_tool()
    assert mod._is_reddish(mod._rgb("#C81E1E")) is True      # 正常红色 → 判红
    assert mod._is_reddish(mod._rgb("#15803D")) is False     # 绿色 → 判非红（会被抓）
    assert mod._is_greenish(mod._rgb("#15803D")) is True     # 正常绿色 → 判绿
    assert mod._is_greenish(mod._rgb("#C81E1E")) is False    # 红色 → 判非绿（会被抓）
    return True
