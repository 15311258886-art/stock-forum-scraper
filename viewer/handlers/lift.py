# -*- coding: utf-8 -*-
"""lift 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

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
    lift_mod,
)

def api_lift_flags(codes: str = "", limit: int = 100) -> dict:
    """限售解禁风险接口（2026-10-30，进哥需求）。

    不传 codes → 全市场未来 60 个交易日内的事件（按剩余交易日升序，截 limit 条）；
    传 codes   → 只算这批（逗号或空格分隔）。

    分档依据全在 `viewer/lift.py` 顶部的实证：首发原股东 & 占流通≥3% 解禁前 20 个
    交易日跑输大盘 3.36%（t=-5.09）。**前 40 个交易日仍为超额上涨**，所以 60 日内只作
    watch 信息提示，不亮红灯 —— 提前三个月提示会让人踏空。
    """
    if lift_mod is None:
        return {"ok": False, "reason": "lift 模块未加载（viewer/lift.py 缺失）"}
    if codes:
        lst = [c.strip() for c in re.split(r"[,\s]+", codes) if c.strip()][:2000]
        return lift_mod.summary(DB.conn, lst)
    cached = _api_cache_get("lift_all")
    if cached is not None:
        d = dict(cached)
    else:
        d = dict(lift_mod.summary(DB.conn))
        d["items"] = {k: v for k, v in d["items"].items() if v["level"]}
        d["n_hit"] = len(d["items"])
        _api_cache_put("lift_all", d, 1800)   # 解禁事件一天只变一次，30 分钟缓存足够
    ordv = {"high": 0, "mid": 1, "watch": 2}
    items = sorted(d["items"].items(),
                   key=lambda kv: (ordv.get(kv[1]["level"], 3), kv[1]["td"]))
    items = items[:max(1, min(limit, 500))]
    return {**d, "items": dict(items), "shown": len(items)}
