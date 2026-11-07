# -*- coding: utf-8 -*-
"""bili 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    bili_methods,
    bili_scan,
)

def api_bili_pile(cat: str | None = None, blogger: str | None = None,
                  limit: int = 2000) -> dict:
    """B站视频分堆列表（technical/value/sentiment/weekly）。

    供前端三处使用：① 消息与外围→市场情绪（cat=sentiment）；
    ③ 复盘成长→技术·价值教学（cat=technical,value）；④ 博主荐股面板合并「下周当红」
    （cat=weekly）。reco 堆已在 blogger_reco，此处不重复。
    """
    out = {"total": 0, "rows": []}
    where, args = [], []
    if cat:
        cats = [c.strip() for c in str(cat).split(",") if c.strip()]
        if cats:
            ph = ",".join("?" * len(cats))
            where.append("cat IN (%s)" % ph)
            args += cats
    if blogger:
        where.append("blogger = ?")
        args.append(blogger)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    tot = DB.conn.execute("SELECT COUNT(*) AS n FROM bili_pile" + wsql, args).fetchone()
    out["total"] = tot["n"] if tot else 0
    lim = max(1, min(int(limit or 2000), 5000))
    rows = DB.conn.execute(
        "SELECT cat, blogger, bvid, title, vdate, url FROM bili_pile" + wsql +
        " ORDER BY vdate DESC LIMIT " + str(lim), args).fetchall()
    out["rows"] = [dict(r) for r in rows]
    return out
def api_bili_methods(cat: str = "technical,value") -> dict:
    """B站「技术·价值教学」方法卡片聚合（2026-10-11 王总需求）。

    每个方法 = 短名 + 一句话总结 + 视频统计 + 映射到的已回测规则真实数字
    （strategy_sim_window·mid 近两年胜率/收益/评级）。映射不到规则的诚实标未回测。
    """
    return bili_methods.methods_payload(DB.conn, cat=cat)
def api_bili_scan(force: bool = False) -> dict:
    """一键筛股（2026-10-11 王总需求）：当前全市场哪些股票正走在知识库战法的买点上。

    结论为主、解释可下钻：返回命中票清单（股票 + 命中战法 + 该战法历史「入手值得率」），
    战法名可点开看解释。后端状态机单飞 + 当日缓存，前端 4s 轮询拿 warming/done。
    首次/缓存失效触发后台扫描（≈2~3 分钟），期间返回进度。
    """
    return bili_scan.scan_payload(DB.conn, force=bool(force))
