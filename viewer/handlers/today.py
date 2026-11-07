# -*- coding: utf-8 -*-
"""today 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    _swr_serve,
    _td_ensure_gate_fields,
    _today_decision_compute,
)

def api_today_decision(force: bool = False) -> dict:
    """今日决策首页（体验审计 P0-A）：一屏四卡（方向/机会/风险/我的票）+ 一句话结论。

    2026-10-30「盘后复盘优先」节奏重构：实测冷态 **52~57s**、热态 16.8s（还出现过 25s 超时），
    且结果不落磁盘 —— 进程重启后第一次点开就在请求线程里干等半分钟以上，而这正是
    用户盘后复盘要看的第一个面板。现将整套计算挪到 `_today_decision_compute`，
    外面套 dashboard 同款 SWR：新鲜缓存直接回 / 重启读盘快照（标 _stale + _cache_age）/
    后台单飞重算。`force=True`（前端手动刷新，`?force=1` / `?refresh=1` / 带 `t=`）可穿透。
    """
    return _td_ensure_gate_fields(
        _swr_serve("today_decision", _today_decision_compute, ttl=1800, force=force))
