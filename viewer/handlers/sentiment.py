# -*- coding: utf-8 -*-
"""sentiment 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    _named,
)

def api_sentiment() -> dict:
    dist = {"看多": 0, "看空": 0, "中性": 0}
    for r in DB.conn.execute("SELECT sentiment, COUNT(*) n FROM posts GROUP BY sentiment").fetchall():
        dist[r["sentiment"]] = r["n"]
    # 情绪地图：按股票聚合多空（同时统计提及总数，便于判断"多0 空0"是中性还是无数据）
    by_stock = {}
    for r in DB.conn.execute(
        """SELECT sm.symbol, sm.symbol_name, p.sentiment
           FROM stock_mentions sm JOIN posts p ON p.id = sm.post_id_ref"""
    ).fetchall():
        s = r["symbol"]
        d = by_stock.setdefault(s, {"bull": 0, "bear": 0, "neu": 0, "total": 0,
                                    "name": r["symbol_name"] or ""})
        d["total"] += 1
        if r["sentiment"] == "看多":
            d["bull"] += 1
        elif r["sentiment"] == "看空":
            d["bear"] += 1
        else:
            d["neu"] += 1
    map_list = []
    for s, v in by_stock.items():
        n = _named(s, v["name"])
        map_list.append({
            "symbol": s,
            "name": n["name"],
            "company": n["company"],
            "market": n["market"],
            "name_missing": n["name_missing"],
            "bull": v["bull"], "bear": v["bear"], "neutral": v["neu"], "mentions": v["total"],
        })
    map_list.sort(key=lambda x: (x["bull"] + x["bear"], x["mentions"]), reverse=True)
    named = sum(1 for x in map_list if not x["name_missing"])
    return {
        "distribution": dist,
        "map": map_list[:30],
        # 名称解析覆盖率：界面据此提示"是否有代码未识别"
        "name_covered": named,
        "name_total": len(map_list),
        "legend": {
            "code": "6 位数字为股票代码（中国内地交易所，沪市 60/68 开头，深市 00/30 开头，北交所 43/83/87 开头）",
            "name": "卡片标题为该股票的股票简称，副标题为公司全称",
            "bull": "「多 N」= 该股被 N 条看多情绪的帖子提及",
            "bear": "「空 N」= 该股被 N 条看空情绪的帖子提及",
            "neutral": "「中性 N」= 情绪词库未判出明确多空的提及条数",
        },
    }
