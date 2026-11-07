# -*- coding: utf-8 -*-
"""dragon 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging

def api_dragon_map() -> dict:
    """GET /api/dragon_map —— 全市场龙头分级的轻量映射 {code: 3金/2银/1铜} + meta。

    为什么只给 tier 不给明细：全市场 5500+ 只，明细带上六项原值有 1MB+；
    徽章只需要 tier，明细点开单只再取（见 api_dragon_one）。
    """
    if dragon_mod is None:
        return {"ok": False, "reason": "龙头分级模块未加载", "m": {}, "meta": {}}
    try:
        if dragon_mod.age_hours() > 24:
            dragon_mod.build_async()      # 过期先给旧数据，后台重建（不阻塞前端）
    except Exception:  # noqa: BLE001
        logging.warning("[dragon] 过期重建触发失败", exc_info=True)
    return dragon_mod.map_payload()
def api_dragon_one(code: str) -> dict:
    """GET /api/dragon?code=600519 —— 单只龙头明细（行业第几/共几家/六项分项排名）。"""
    if dragon_mod is None:
        return {"ok": False, "reason": "龙头分级模块未加载"}
    return dragon_mod.one(code)
