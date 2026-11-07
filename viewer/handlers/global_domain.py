# -*- coding: utf-8 -*-
"""global 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging
from viewer.server import (  # noqa: E402,F401
    global_market,
)

def api_global_market() -> dict:
    """全球大盘 / 海外市场（「消息与外围」）：美股指数 + 美股行业板块 + 主要经济体 +
    港股 + 跨资产风向标。读 viewer/global_market.py 的本地缓存（国内免费源、免 cookie）。

    口径诚实边界：美股为真实指数；日、韩、德、英、法、中国台湾、印度、巴西等为
    **美股上市国家 ETF 代理**（字段 proxy=True，含汇率与美股情绪因素），
    方向可参考、点位不等同本地指数。详见 global_market.py 模块头。
    """
    try:
        return global_market.payload(auto=True)
    except Exception as e:  # noqa: BLE001
        # 2026-10-31 修复：原为 `log.warning(...)`，但本函数作用域内没有 `log`
        # （server.py 模块级也没有）→ 异常处理自身抛 NameError，把真实错误盖成 500。
        logging.getLogger(__name__).warning("global_market 接口失败: %s", e)
        return {"ok": False, "error": str(e)[:160], "indices": [], "sectors": [],
                "themes": [], "cross_asset": [], "countries": [], "hk": [], "summary": {}}
