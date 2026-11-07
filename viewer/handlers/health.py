# -*- coding: utf-8 -*-
"""health 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging
from viewer.server import (  # noqa: E402,F401
    HEALTH,
    _brake_leases_view,
)

def api_health() -> dict:
    """/api/health —— 任务运行记录 + 数据新鲜度（决策⑤）。

    回答两个此前没人回答的问题：
      · 调度任务到底有没有在成功？此前失败只写日志，而日志没人看
        （build_exclusion 11 次全失败、a_shares_snapshot 连失两天记成完成）；
      · 数据到底停在哪一天？此前只能靠"数字看着不对"察觉。

    `level` 有四个值，`unknown` **不等于正常** —— 判不出就说不知道，
    这是本项目反复强调的同一条原则（不要用默认值把未知盖住）。
    内部 60 秒缓存：新鲜度按交易日变，轮询面板不需要每次重扫 1.7 亿行。

    2026-11-01（P0 租约泄漏）：新增 `leases` 段（见 `_brake_leases_view`）。
    它**不进** 60 秒缓存 —— 锁状态必须实时，否则"租约还在不在"这个问题的答案会滞后。
    """
    try:
        out = dict(HEALTH.overall())
    except Exception as e:  # noqa: BLE001
        logging.exception("api_health 失败")
        return {"ok": False, "error": "health_failed", "detail": str(e)}
    try:
        out["leases"] = _brake_leases_view()
    except Exception as e:  # noqa: BLE001
        # 观测段自己坏掉不能把整个 health 拖垮（前端靠它判"系统是否正常"）
        logging.exception("api_health 的 leases 段失败")
        out["leases"] = {"ok": False, "error": str(e)[:200],
                         "items": [], "recent_refusals": [], "domains_now": []}
    return out
