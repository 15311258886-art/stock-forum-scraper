# -*- coding: utf-8 -*-
"""sync 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    get_resolver,
)

def api_sync_names(symbols: list[str] | None = None, online: bool = True, force: bool = False) -> dict:
    """补全股票名称：离线表优先，缺失的联网（东方财富）查询后落库缓存，并回填历史记录。
    force=True 时对已识别的代码也重新联网核对（用于股票更名等场景）。"""
    codes = symbols or DB.all_symbols()
    res = get_resolver(db=DB).sync(codes, online=online) if force else \
        get_resolver(db=DB).backfill_missing(codes, online=online)
    return {"requested": res["requested"], "resolved": res["resolved"], "persisted": res["persisted"],
            "items": res["items"]}
