# -*- coding: utf-8 -*-
"""raw 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_raw_pages(limit: int = 60) -> dict:
    """原始存档（知识获取 → 原始存档）：抓取回来的原始页面清单。

    只暴露「存档元信息」——来源、URL、抓取时刻、字节数与正文长度；
    **不回正文**，避免把整页 HTML 塞进接口（面板只需要知道存了什么、什么时候存的）。
    """
    limit = max(1, min(int(limit or 60), 500))
    rows = DB.conn.execute(
        """SELECT id, site, url, fetched_at, http_status,
                  LENGTH(COALESCE(html,'')) AS bytes
           FROM raw_pages ORDER BY fetched_at DESC, id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    total = DB.conn.execute("SELECT COUNT(*) AS n FROM raw_pages").fetchone()["n"]
    by_site = {r["site"]: r["n"] for r in DB.conn.execute(
        "SELECT site, COUNT(*) AS n FROM raw_pages GROUP BY site ORDER BY n DESC")}
    return {"total": total, "shown": len(rows), "by_site": by_site,
            "pages": [dict(r) for r in rows]}
