# -*- coding: utf-8 -*-
"""policy 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import threading, time
from viewer.server import (  # noqa: E402,F401
    DB,
    _safe_policy_refresh,
    policydesk,
)

def api_policy_desk(num: int = 80) -> dict:
    # 非阻塞：HTTP 线程只做库读，外部抓取（gov.cn + 中央级快讯筛选 + 板块代表股）甩给后台线程，
    # 首屏不再卡在冷抓取上；若库内尚无数据，payload 返回 warming=True，前端 3s 后补取。
    try:
        if time.time() - getattr(policydesk, "_LAST_REFRESH", 0.0) >= policydesk.REFRESH_TTL:
            threading.Thread(target=_safe_policy_refresh, name="policy-prewarm", daemon=True).start()
    except Exception:  # noqa: BLE001
        pass
    return policydesk.payload(DB.conn, num=num)
