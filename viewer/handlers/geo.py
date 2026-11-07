# -*- coding: utf-8 -*-
"""geo 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    geo_store,
)

def api_geo_events(category: str | None = None, days: int = 14, panel: str | None = None,
                   news_days: int = 10, news_limit: int = 40) -> dict:
    """国际局势面板数据：研判事件（人工+AI 联网调研，自带方向）+ 国际新闻流（只分类不给方向）。

    诚实口径（硬性）：
      · events 里的 direction / sectors / symbols 是**研判**，不是事实，标注"研判"；
      · 带 cross_check 的事件给出国内外信源交叉印证（一致/分歧/待观察）；
      · news 是 overseas_news 里筛出的国际类条目，**只做机器初判分类**，明确标"未研判"，
        不对其下方向结论（避免把抓取噪声当成判断抛给用户）。
    """
    geo_store.ensure_schema(DB.conn)
    events = geo_store.query_events(DB.conn, category=category, days=days, panel=panel, limit=200)
    news = geo_store.recent_intl_news(DB.conn, days=news_days, limit=news_limit)
    return {
        "stats": geo_store.stats(DB.conn),
        "events": events,
        "news": news,
        "meta": {
            "honesty": ("数据获取（2026-10-20 起自动/半自动）：本地域自动抓取+自动调度；海外舆情每日自动安全刷新（联网成功才写入、失败保留旧数据）；"
                        "国际局势事件库每周一/四 09:30 由 AI 联网调研更新后入库——日常无需手动操作，兜底入口在顶栏「一键刷新」。"
                        "研判事件含方向判断，为观点而非事实，所列个股仅作代表性观察标的、不构成投资建议；"
                        "带\"印证\"标签的事件为国内外信源交叉核对结果；下方新闻流仅做机器分类、未做方向研判。"),
        },
    }
def api_geo_refresh() -> dict:
    """一键刷新：幂等重导 seed_events.json（按 eid upsert）+ 重建国际新闻流视图。

    说明（诚实）：本接口不自动联网爬取——国际研判事件由人工+AI 联网调研沉淀进 seed_events.json，
    更新该文件后点此接口即可重新入库；新闻流直接重读 overseas_news 现成数据。
    真正的"联网增量调研"是离线步骤（WebSearch 服务端通道），沉淀进 seed 后由本接口落库。
    """
    geo_store.ensure_schema(DB.conn)
    imp = geo_store.import_seed(DB.conn)
    stats = geo_store.stats(DB.conn)
    return {
        "ok": True,
        "imported": imp,
        "stats": stats,
        "note": "已幂等重导 seed_events.json；新闻流为重读 overseas_news。如需新增国际研判事件，"
                "请更新 geo/seed_events.json 后再次点此刷新（联网调研为离线步骤）。",
    }
