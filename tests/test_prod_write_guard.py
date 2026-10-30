# -*- coding: utf-8 -*-
"""D33 事故加固：pytest 里绝不允许写生产库/生产日志。

## 事故经过（真实发生）

验证 `lhb_daily`「daemon 线程被短命子进程杀掉」时，我在**同一个 pytest 进程内**把模块文件
换回旧版 → 旧版 `maybe_daily()` 起了一个 daemon 线程 → **测试结束、monkeypatch 撤销之后**
那个孤儿线程才把活干完。此时它看到的 `DB` / `LOG_FILE` 已经恢复成**生产路径**，于是：

- `viewer/logs/lhb_daily.log` 多出 4 条 `started=2026-10-30 18:00:00 … 484.0s`（一眼假的桩数据）
- `meta.lhb_daily_state.last_date` 被推成 `2026-10-30`

后果比"多写几行"严重得多：`_gate` 的闸门是 `last_date`，当晚 18:00 会判 `skip: done_today`，
**真正该跑的那一轮被自己的测试挡掉了** —— 把刚修好的 11 天停更 bug 原样复刻。

## 两道防线

1. **写盘守卫**（本文件测的）：pytest 环境下、且路径仍是生产路径 → 只 warning、不落盘。
   路径是 import 时快照的（`_PROD_DB` / `_PROD_LOG`），所以 monkeypatch 改了 `DB` 也不会
   让"生产"这个判断跟着漂。
2. **测试侧根治**：红态证明改到子进程里跑并一开始就把路径指到 tmp，
   见 `tests/test_lhb_daily_offproc.py::test_child_process_finishes_work_before_exiting`。
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _in_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _boom(*a, **k):  # noqa: ARG001
    raise AssertionError("守卫失效：测试竟然走到了真正写盘的那一步")


# ---------------------------------------------------------------- 谓词本身
def test_predicate_blocks_prod_paths():
    ld = importlib.import_module("lhb_daily")
    assert _in_pytest(), "PYTEST_CURRENT_TEST 没设，本组测试无意义"
    assert ld._prod_write_blocked(ld.DB, ld._PROD_DB) is True
    assert ld._prod_write_blocked(ld.LOG_FILE, ld._PROD_LOG) is True


def test_predicate_allows_tmp_and_relaxed_outside_pytest(monkeypatch, tmp_path):
    ld = importlib.import_module("lhb_daily")
    assert ld._prod_write_blocked(str(tmp_path / "app.db"), ld._PROD_DB) is False
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert ld._prod_write_blocked(ld._PROD_DB, ld._PROD_DB) is False, (
        "非 pytest 环境（真实运行）必须放行，否则日更永远写不进库")


def test_prod_paths_are_snapshotted_not_live():
    """`_PROD_DB` 必须是 import 时的快照 —— 它是"生产"这个概念的锚点。"""
    ld = importlib.import_module("lhb_daily")
    assert ld._PROD_DB == os.path.abspath(os.path.join(ROOT, "viewer", "app.db"))
    assert ld._PROD_LOG == os.path.abspath(os.path.join(ROOT, "viewer", "logs", "lhb_daily.log"))
    bd = importlib.import_module("blogger_daily")
    assert bd._PROD_DB == os.path.abspath(os.path.join(ROOT, "viewer", "app.db"))
    assert bd._PROD_LOG == os.path.abspath(os.path.join(ROOT, "viewer", "logs", "blogger_daily.log"))


# ---------------------------------------------------------------- 行为：真的不落盘
def test_lhb_record_result_does_not_touch_prod_db(monkeypatch):
    """指向生产库时，`record_result` 必须**在连库之前**就返回。"""
    ld = importlib.import_module("lhb_daily")
    monkeypatch.setattr(ld, "DB", ld._PROD_DB)              # 指向生产
    monkeypatch.setattr(ld, "sqlite3", types.SimpleNamespace(connect=_boom))
    ld.record_result({"ok": True, "started": "s", "ended": "e", "steps": []},
                     "2026-10-30")                          # 不该抛、也不该连库


def test_lhb_append_log_does_not_touch_prod_log(monkeypatch):
    ld = importlib.import_module("lhb_daily")
    monkeypatch.setattr(ld, "LOG_FILE", ld._PROD_LOG)
    monkeypatch.setattr(ld, "open", _boom, raising=False)   # 模块级 open 影子
    ld._append_result_log({"ok": True, "started": "s", "ended": "e", "steps": []})


def test_blogger_record_result_and_log_guarded(monkeypatch):
    bd = importlib.import_module("blogger_daily")
    monkeypatch.setattr(bd, "DB", bd._PROD_DB)
    monkeypatch.setattr(bd, "sqlite3", types.SimpleNamespace(connect=_boom))
    bd._record_result({"returncode": 0, "seconds": 1.0}, "2026-10-30", "test")

    monkeypatch.setattr(bd, "LOG_FILE", bd._PROD_LOG)
    monkeypatch.setattr(bd, "open", _boom, raising=False)
    bd._append_result_log({"returncode": 0, "seconds": 1.0}, "test")


def test_guarded_write_still_works_on_tmp(monkeypatch, tmp_path):
    """反向：指向 tmp 时必须正常工作（否则等于把功能改死）。"""
    ld = importlib.import_module("lhb_daily")
    monkeypatch.setattr(ld, "DB", str(tmp_path / "app.db"))
    monkeypatch.setattr(ld, "LOG_FILE", str(tmp_path / "lhb_daily.log"))
    ld.record_result({"ok": True, "started": "2026-10-30 18:00:00",
                      "ended": "2026-10-30 18:08:04",
                      "steps": [{"script": "lhb_fetch.py", "returncode": 0, "seconds": 1.0}]},
                     "2026-10-30")
    assert (tmp_path / "lhb_daily.log").exists(), "tmp 路径应正常写日志"
    import sqlite3
    con = sqlite3.connect(tmp_path / "app.db")
    try:
        assert ld._state_read(con).get("last_date") == "2026-10-30"
    finally:
        con.close()
