# -*- coding: utf-8 -*-
"""freshness 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import datetime
from viewer.server import (  # noqa: E402,F401
    DB,
)

def api_freshness() -> dict:
    """P0-3（2026-10-15）：统一「数据保鲜状态」接口。

    返回各**非实时**数据域的「最后成功更新时间」，供前端渲染「🕓 最后更新 X · N 天前」
    陈旧角标（>3 天变灰）。计算全部走 MAX(数据时间列)，表/列不存在时优雅返回 None，
    不影响其它域。注意：一律取「数据发生时间列」（event_date / pub_date / as_of_date / day）
    而非「入库时间列」（updated_at）——否则离线重导会把角标刷成假新鲜。
    """
    def _max(sql: str):
        try:
            r = DB.conn.execute(sql).fetchone()
            return (r[0] if r else None) or None
        except Exception:
            return None

    now = datetime.now()

    def _days_ago(v):
        if not v:
            return None
        s = str(v)[:10]
        try:
            d = datetime.strptime(s, "%Y-%m-%d").date()
            return (now.date() - d).days
        except Exception:
            return None

    domains = {
        # 2026-10-31（D51）：论坛语料水位。这是全站「数据到底停在哪天」的第一个来源——
        #   此前只有各面板内部的「数据截至」，用户要逐页点开才知道手里这份东西新不新。
        #   盘后与周末不采集属正常，故不设 action（不需要「点击补齐」）。
        "forum": {
            "label": "论坛语料（淘股吧 / 东财）",
            "updated_at": _max("SELECT MAX(post_time) FROM posts"),
            "source": "本机采集器（交易时段每约 10 分钟抓一次；盘后与周末不抓）",
            "realtime": False,
        },
        "geo": {
            "label": "国际局势 / 全球政策 / 科技前沿",
            "updated_at": _max("SELECT MAX(event_date) FROM geo_events"),
            "source": "geo/seed_events.json（离线重导，联网调研为离线步骤）",
            "realtime": False,
            "action": "geo_refresh",
            "autofill": False,
            "hint": "国际局势事件库已由 WorkBuddy 每周一/四 09:30 自动联网调研更新（本机只需开机联网）；点此可立即重导最新事件库。",
        },
        "public_records": {
            "label": "公开实盘战绩",
            "updated_at": _max("SELECT MAX(as_of_date) FROM public_records"),
            "source": "public_records_seed.json（离线重导）",
            "realtime": False,
        },
        "overseas_sentiment": {
            "label": "美股港股舆情",
            "updated_at": _max("SELECT MAX(day) FROM overseas_sentiment_daily"),
            "source": "overseas 采集（手动「刷新海外舆情(联网)」触发，现每日自动重试）",
            "realtime": False,
            "action": "overseas_collect",
            "autofill": True,
            "hint": "联网采集依赖 VPN；点击将重新抓取（联网不通会自动保留旧数据）。",
        },
        "overseas_hotspots": {
            "label": "国外驱动热点",
            "updated_at": _max("SELECT MAX(updated_at) FROM overseas_hotspots"),
            "source": "overseas 采集（手动「刷新海外舆情(联网)」触发，现每日自动重试）",
            "realtime": False,
            "action": "overseas_collect",
            "autofill": True,
            "hint": "联网采集依赖 VPN；点击将重新抓取（联网不通会自动保留旧数据）。",
        },
    }
    for k, v in domains.items():
        v["days_ago"] = _days_ago(v.get("updated_at"))
        v["stale"] = bool(v.get("days_ago") is not None and v["days_ago"] > 3)
    return {"domains": domains, "generated_at": now.strftime("%Y-%m-%d %H:%M:%S")}
