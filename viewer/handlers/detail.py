# -*- coding: utf-8 -*-
"""detail 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_detail(kind: str, _id: int, full: bool = False) -> dict:
    if kind == "post":
        if full:
            # 仅在用户显式点击「查看原始文档」时返回完整原始页面 HTML（后台库完整保留）。
            row = DB.conn.execute(
                "SELECT p.id, p.site, p.author, p.post_id, p.post_url, p.post_time, p.text, "
                "p.sentiment, p.sentiment_score, rp.html AS raw_html "
                "FROM posts p LEFT JOIN raw_pages rp ON rp.id=p.raw_page_id WHERE p.id=?",
                (_id,),
            ).fetchone()
            return dict(row) if row else {"error": "not found"}
        # 默认：仅结构化字段，不返回可能数 MB 的原始页面，避免前端卡顿。
        row = DB.conn.execute(
            "SELECT id, site, author, post_id, post_url, post_time, text, sentiment, sentiment_score "
            "FROM posts WHERE id=?", (_id,),
        ).fetchone()
    elif kind == "trade":
        row = DB.conn.execute(
            "SELECT id, site, author, post_id, post_url, post_time, action, symbol, symbol_name, price, quantity, raw_text "
            "FROM trades WHERE id=?", (_id,)).fetchone()
    elif kind == "news":
        row = DB.conn.execute("SELECT * FROM news WHERE id=?", (_id,)).fetchone()
    else:
        return {"error": "unknown kind"}
    return dict(row) if row else {"error": "not found"}
def api_detail_list(kind: str, limit: int = 500, offset: int = 0) -> dict:
    """返回某个统计项对应的明细记录（供统计卡片点击查看）。

    total 与 /api/summary 的 counts 采用同一 COUNT(*) 口径，保证数字与列表一致。
    """
    spec = _DETAIL_SPECS.get(kind)
    if not spec:
        return {"error": "unknown kind", "kind": kind,
                "available": sorted(_DETAIL_SPECS.keys())}
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    table = spec["table"]  # 常量，非外部输入
    total = DB.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    rows = DB.conn.execute(spec["sql"] + " LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return {
        "kind": kind, "label": spec["label"], "total": total,
        "returned": len(rows), "limit": limit, "offset": offset,
        "columns": spec["columns"], "drill": spec.get("drill"),
        "rows": [dict(r) for r in rows],
    }
