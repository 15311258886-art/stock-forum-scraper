# -*- coding: utf-8 -*-
"""overseas 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import json
from viewer.server import (  # noqa: E402,F401
    DB,
    _overseas_cfg,
    _overseas_insight,
    _overseas_recent_news,
    _zh_hotspot_title,
)

def api_overseas_sentiment() -> dict:
    """海外舆情监控（模块一）：整体情绪、按区域分布、按日×区域情绪序列（趋势）、
    数据源构成、重点标的、近期新闻（国外源优先排序）。"""
    rows = DB.conn.execute(
        "SELECT sentiment, COUNT(*) n, SUM(heat) heat FROM overseas_news GROUP BY sentiment").fetchall()
    dist = {"看多": 0, "看空": 0, "中性": 0}
    heat_total = 0.0
    for r in rows:
        dist[r["sentiment"]] = r["n"]
        heat_total += float(r["heat"] or 0)
    total = sum(dist.values())
    net = round((dist["看多"] - dist["看空"]) / total * 100, 1) if total else 0.0

    reg: dict[str, dict] = {}
    for r in DB.conn.execute(
        "SELECT region, COUNT(*) n, SUM(heat) heat FROM overseas_news GROUP BY region").fetchall():
        reg[r["region"]] = {"count": r["n"], "heat": round(float(r["heat"] or 0), 2)}

    daily = [dict(r) for r in DB.conn.execute(
        "SELECT day,region,bull,bear,neutral,total,heat,mood "
        "FROM overseas_sentiment_daily ORDER BY day, region").fetchall()]

    src = [dict(r) for r in DB.conn.execute(
        "SELECT source_name, region, kind, COUNT(*) n, SUM(heat) heat "
        "FROM overseas_news GROUP BY source_name, region, kind ORDER BY n DESC").fetchall()]

    # 近期新闻：按区域配额取样（US / HK / GLOBAL 均需出现），避免单区垄断
    news = _overseas_recent_news()

    symbols = DB.overseas_symbols()
    cfg = _overseas_cfg()
    return {
        "distribution": dist, "net_mood": net, "heat_total": round(heat_total, 2), "total": total,
        "region_dist": reg, "daily": daily, "source_mix": src, "news": news,
        "symbols": symbols, "region_weights": cfg.get("region_weights") or {},
        "topics": list((cfg.get("topics") or {}).keys()),
        # 决策导向的再组织：他们在说什么 / 注意力投向哪 / 做了什么 / 对内陆的影响与建议
        "insight": _overseas_insight(),
    }
def api_overseas_hotspots() -> list[dict]:
    """近期热点（模块二）：由国外新闻驱动，自动汇总的事件 + 提升性影响分析（传导路径 + 放大/衰减）。"""
    rows = DB.conn.execute("SELECT * FROM overseas_hotspots ORDER BY heat DESC").fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        # 结构化 JSON 字段（evidence 为历史字段，保留解析以兼容旧库）
        # sectors_up_detail / sectors_down_detail：按细分领域分组的代表公司
        #   [{"sector":"AI算力","symbols":[{"code":"300308","name":"中际旭创"}]}]
        for k in ("evidence", "sectors_up_detail", "sectors_down_detail"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except Exception:
                d[k] = []
        for k in ("driver_sources", "driver_regions", "domestic_sectors", "related_markets",
                  "related_symbols", "variables", "sectors_up", "sectors_down"):
            v = d.get(k) or ""
            d[k] = [x for x in v.split(",") if x] if isinstance(v, str) else (v or [])
        # ① 驱动源清洗：「示例源·xx」是结构化示例（种子），并非真实接入源。
        #    旧库已把它写进 driver_sources（实测 15/17 条热点都混着），展示层再剔一次，
        #    否则卡片上会显示 20+ 家"驱动源"，其中一半是假的。
        d["driver_sources"] = [x for x in (d.get("driver_sources") or [])
                               if "示例源" not in str(x)]
        # ② 标题中文化（英文原文落到 title_en，供悬停核对）
        _zh, _en = _zh_hotspot_title(d.get("key"), d.get("title"), d.get("topic"))
        d["title"], d["title_en"] = _zh, _en
        out.append(d)
    return out
def api_overseas_sources() -> dict:
    """数据源接入状态（界面如实展示「已接入 / 待激活」）与权重配置。"""
    rows = [dict(r) for r in DB.conn.execute(
        "SELECT * FROM overseas_sources_status ORDER BY region, name").fetchall()]
    cfg = _overseas_cfg()
    return {"sources": rows, "region_weights": cfg.get("region_weights") or {},
            "topics": list((cfg.get("topics") or {}).keys())}
def api_overseas_collect(live: bool = True, reset: bool = True) -> dict:
    """显式刷新海外舆情（可联网 live=True，或仅用结构化示例 live=False）。"""
    try:
        from overseas.collect import collect
        return collect(db=DB, include_seed=True, reset=reset, live=live)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
