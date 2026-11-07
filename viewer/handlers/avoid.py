# -*- coding: utf-8 -*-
"""avoid 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import re
from viewer.server import (  # noqa: E402,F401
    DB,
    _api_cache_get,
    _api_cache_put,
    avoid_mod,
)

def api_avoid_flags(codes: str = "", limit: int = 100) -> dict:
    """避坑标记接口（2026-10-30，替代已下线的「博主反向警示」）。

    不传 codes → 当日全市场命中票（按分降序，截 limit 条），供风险榜/自检；
    传 codes   → 只算这批（逗号或空格分隔），关注池/手输代码都可直接查。

    结果全字段来自 `viewer/avoid.py`：当日量价 + 近 10 日龙虎榜 + 近 20 日减持公告，
    **不含任何博主数据**，也不读任何沉淀快照。
    """
    if avoid_mod is None:
        return {"ok": False, "reason": "avoid 模块未加载（viewer/avoid.py 缺失）"}
    if codes:
        lst = [c.strip() for c in re.split(r"[,\s]+", codes) if c.strip()][:2000]
        return avoid_mod.summary(DB.conn, lst)
    cached = _api_cache_get("avoid_all")
    if cached is not None:
        d = dict(cached)
    else:
        all_codes = [r[0] for r in DB.conn.execute(
            "SELECT code FROM stock_indicators_daily WHERE date="
            "(SELECT MAX(date) FROM stock_indicators_daily)").fetchall()]
        d = dict(avoid_mod.summary(DB.conn, all_codes))
        d["items"] = {k: v for k, v in d["items"].items() if v["level"]}
        d["n"], d["n_hit"] = len(all_codes), len(d["items"])
        _api_cache_put("avoid_all", d, 300)
    items = sorted(d["items"].items(), key=lambda kv: -kv[1]["score"])[:max(1, min(limit, 500))]
    return {**d, "items": dict(items), "shown": len(items)}
