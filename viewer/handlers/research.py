# -*- coding: utf-8 -*-
"""research 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import threading
from viewer.server import (  # noqa: E402,F401
    _RI_LOCK,
    _ri_warm,
    research,
    research_insight,
    _RI_STATE,
)

def api_research_reports(pz: int = 30, page: int = 1) -> dict:
    return research.report_list(pz=pz, page=page)
def api_research_rating(symbol: str) -> dict:
    return research.rating(symbol)
def api_research_insight(days: int = 60, force: bool = 0) -> dict:
    with _RI_LOCK:
        # 关键：后台正在重算时，哪怕是「不带 force 的普通轮询」也必须返回 warming。
        # 否则前端 4 秒一问都拿到内存里的旧结论，永远进不了「生成中」提示，
        # 用户看到的是旧数字，等新结论出来又是另一套 —— 看不到过程也校不上。
        if force or _RI_STATE["running"] or _RI_STATE["data"] is None:
            if not _RI_STATE["running"]:
                _RI_STATE["running"] = True
                threading.Thread(target=_ri_warm, args=(days,), daemon=True).start()
            return {"ok": False, "warming": True, "reason": "结论生成中（需抓 600 份研报，约 1 分钟），请稍后重试"}
        d = dict(_RI_STATE["data"])
    d["cached"] = not force
    return d
def api_research_insight_rebuild(days: int = 60) -> dict:
    research_insight.insight(days=days, force=True)   # 同步重算并落缓存
    with _RI_LOCK:
        _RI_STATE["data"] = None
    with _RI_LOCK:
        _RI_STATE["data"] = research_insight.insight(days=days, force=True)
    return api_research_insight(days=days)
