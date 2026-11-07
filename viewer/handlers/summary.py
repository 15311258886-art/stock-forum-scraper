# -*- coding: utf-8 -*-
"""summary 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    _player_cfg,
    _site_login_meta,
    _sites,
)

def api_summary() -> dict:
    counts = DB.counts()
    login = DB.get_login_status()
    # 给每条登录态补充「登录网址 + 中文名 + 是否需要登录」，使前端能渲染可点击的登录入口。
    login = [
        {**row, **_site_login_meta(row.get("site") or "")}
        for row in login
    ]
    last = DB.get_meta("last_ingest", "尚未抓取")
    return {"counts": counts, "login_status": login, "last_ingest": last,
            # 「知识获取 → 抓取与登录记录」用：最近一次抓取动作的执行时刻
            "last_fetch_at": DB.get_meta("last_fetch_at", ""),
            "sites": _sites(),
            # 2026-10-05（报告 P2 / 建议4）：把「选手榜 / 持仓采集」的配置可用性显性化。
            # 这两项从来没配全过，界面上却是完整面板 —— 得让用户看见它是半启用状态。
            "player_cfg": _player_cfg()}
