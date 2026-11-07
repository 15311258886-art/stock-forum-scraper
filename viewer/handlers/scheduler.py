# -*- coding: utf-8 -*-
"""scheduler 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import datetime
from viewer.server import (  # noqa: E402,F401
    _SCHED_MIN_GAP,
    _parse_dt_loose,
    _sched_in_window,
    _sched_load_state,
    _sched_next_window,
    _sched_sources,
    timedelta,
)

def api_scheduler_status() -> dict:
    """/api/scheduler/status 返回：{enabled, last_run_at, next_run_at, run_count, errors_today}。"""
    state = _sched_load_state()
    now = datetime.now()
    enabled = bool(state.get("enabled", False))
    last_run = state.get("last_run_at")
    srcs = _sched_sources()
    if not enabled:
        next_run = None
    elif not srcs:
        # 源已停用：说「下次不跑」，而不是按 last_run_at 算出一个『马上会跑』的时刻。
        # 原实现在 last_run_at 为 None 时返回 now，界面因此显示「即将运行」，
        # 而实际上永远不会运行 —— 这是本轮要求消灭的「确定性与不确定性配比颠倒」的一个实例。
        next_run = None
    elif not _sched_in_window(now):
        next_run = _sched_next_window(now).strftime("%Y-%m-%d %H:%M:%S")
    else:
        last = _parse_dt_loose(last_run)
        if last is None:
            next_run = now.strftime("%Y-%m-%d %H:%M:%S")   # 从未跑过，马上会跑
        else:
            cand = last + timedelta(seconds=_SCHED_MIN_GAP)
            next_run = (cand if cand > now else now).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "enabled": enabled,
        "last_run_at": last_run,
        "next_run_at": next_run,
        "run_count": int(state.get("run_count", 0)),
        "errors_today": int(state.get("errors_today", 0)),
        "in_window": _sched_in_window(now),
        "sources": srcs,
        "sources_empty": not srcs,
        "state_known": bool(state.get("state_known", True)),
        "skip_reason": (None if (enabled and srcs) else
                        ("state_unknown" if not state.get("state_known", True) else
                         ("sources_empty" if not srcs else "disabled"))),
    }
