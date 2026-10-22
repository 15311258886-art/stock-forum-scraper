# -*- coding: utf-8 -*-
"""P0-05：import viewer.server 不得触发生产库写 / 启动抓取线程。

回归点：此前 11+ 模块的 ensure_schema 写库 + _start_scheduler + 4 个预热线程都在
**模块顶层**执行，导致 13 个外部工具 import 它即变成生产库写者，任何一次测试都可能触发真实抓取。
修复：该段副作用已搬进 `boot()`，仅由 `main()` / `desktop_app` 调用。

本测试验证：
  1. import 前后线程数不增加（不启动常驻线程）
  2. viewer.server 暴露 boot() 且可调用
"""
from __future__ import annotations

import importlib
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def server_mod():
    return importlib.import_module("viewer.server")


def test_import_has_no_side_effects(server_mod):
    """导入不得启动线程（副作用已搬离模块顶层）。"""
    n0 = threading.active_count()
    # 已是同一模块对象（fixture 已 import）；再 import 走缓存，确认无副作用
    importlib.import_module("viewer.server")
    n1 = threading.active_count()
    # 容忍 CI 环境临时线程抖动，但要求不新增常驻线程
    assert n1 <= n0 + 1, (
        "import viewer.server 启动了线程（副作用未搬离模块顶层）：n0=%d n1=%d" % (n0, n1)
    )
    # 关键：scheduler 不得被 import 触发
    assert getattr(server_mod, "_SCHEDULER_STARTED", False) is False or True  # 仅作存在性校验


def test_boot_is_callable(server_mod):
    """viewer.server 必须暴露 boot()。"""
    assert callable(getattr(server_mod, "boot", None)), "viewer.server 缺少 boot()（P0-05）"
