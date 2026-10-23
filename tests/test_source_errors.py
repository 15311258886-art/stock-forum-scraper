# -*- coding: utf-8 -*-
"""P2-03 最小单测：请求级数据源错误采集（不 import viewer.server）。

直接验证 viewer.source_errors 的 record / reset / attach 行为——这是后端在 4 个静默
except 分支上抛 source_errors、并在 _send_json 注入响应体的同一套逻辑。
被测模块不依赖 server，故本测试绝不 import viewer.server（符合基线约束）。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer.source_errors import (  # noqa: E402
    record_source_error,
    get_source_errors,
    reset_source_errors,
    attach_source_errors,
)


def test_no_error_no_injection():
    """无错误时不注入 'source_errors' 键，dict 原样返回。"""
    reset_source_errors()
    out = attach_source_errors({"data": []})
    assert "source_errors" not in out, out
    assert out == {"data": []}


def test_single_error_injected():
    """记录一条错误后，dict 响应体被注入 source_errors。"""
    reset_source_errors()
    record_source_error("stock_names", "boom")
    out = attach_source_errors({"data": [1, 2]})
    se = out.get("source_errors")
    assert isinstance(se, list) and len(se) == 1, out
    assert se[0] == {"source": "stock_names", "error": "boom"}
    assert out.get("data") == [1, 2]


def test_multiple_errors_accumulate():
    """多条错误累积。"""
    reset_source_errors()
    record_source_error("stock_holders", "disk full")
    record_source_error("fund_strategy_signal", "timeout")
    assert len(get_source_errors()) == 2


def test_reset_clears():
    """reset 清空。"""
    reset_source_errors()
    record_source_error("a", "1")
    assert len(get_source_errors()) == 1
    reset_source_errors()
    assert get_source_errors() == []


def test_non_dict_passthrough():
    """非 dict 响应体（如列表）不被污染，原样返回。"""
    reset_source_errors()
    record_source_error("x", "y")
    lst = attach_source_errors([1, 2, 3])
    assert lst == [1, 2, 3]


def test_get_source_errors_returns_copy():
    """get_source_errors 返回副本（外部修改不影响内部）。"""
    reset_source_errors()
    record_source_error("a", "1")
    snap = get_source_errors()
    snap.append({"source": "b", "error": "2"})
    assert len(get_source_errors()) == 1


def test_source_error_shape():
    """错误项结构含 source / error 两个字段（前端据此渲染）。"""
    reset_source_errors()
    record_source_error("stock_holder_num", "boom")
    se = get_source_errors()
    assert set(se[0].keys()) == {"source", "error"}
    assert se[0]["source"] == "stock_holder_num"
    assert se[0]["error"] == "boom"
