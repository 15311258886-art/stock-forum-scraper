# -*- coding: utf-8 -*-
"""news 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import re
from viewer.server import (  # noqa: E402,F401
    _news_stock_fetch,
    newsfeed,
)

def api_news_stock(symbol: str, num: int = 30) -> dict:
    """个股快讯：东财公告/快讯（newsfeed.stock）+ 本地情绪研判。
    2026-10-23 性能修复：加「磁盘 30min TTL 缓存 + 并发合并(single-flight)」，
    同 symbol 并发首开只打一次上游，30min 内重复开直接命中磁盘（东财调用=0）。"""
    if cu and symbol:
        code = re.sub(r"\D", "", symbol or "")
        if code:
            return cu.disk_cache(
                "news", code, 1800,
                lambda: cu.coalesce(f"news:{symbol}",
                                    lambda: _news_stock_fetch(symbol, num=num),
                                    label="news"))
    return _news_stock_fetch(symbol, num=num)
def api_news_market(num: int = 30) -> dict:
    return newsfeed.market(num=num)
