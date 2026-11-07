# -*- coding: utf-8 -*-
"""refresh 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

from viewer.server import (  # noqa: E402,F401
    DB,
    REFRESH_INTERVAL_SECONDS,
    REFRESH_STALE_SECONDS,
    SITE_DISPLAY_NAMES,
    _kb_keys,
    _retired_feeds,
    _sched_load_state,
    _sites,
    api_market_freshness,
)

def api_refresh_status() -> dict:
    """刷新状态汇总（Request G）。

    - last_ok_at：最近一次**整轮抓取**成功完成的时刻（= source_key「__all__」）
    - sources：逐来源的最近成功/尝试时刻与状态，供「最后更新」列逐行取用
    - tracked_sources：当前**已纳入刷新**的来源 = 抓取配置里的站点 + 知识库来源。
      未列入的来源不参与时效判定（前端显示「未纳入刷新」），避免误报超时。
    - interval_seconds / stale_seconds：与前端共享的刷新周期与超时阈值
    """
    rows = DB.get_refresh_status()
    by_key = {r["source_key"]: r for r in rows}
    allrow = by_key.get("__all__") or {}
    sources = []
    for key, r in by_key.items():
        if key == "__all__":
            continue
        sources.append({
            "source_key": key,
            "display_name": r.get("display_name") or SITE_DISPLAY_NAMES.get(key, key),
            "last_ok_at": r.get("last_ok_at"),
            "last_attempt_at": r.get("last_attempt_at"),
            "status": r.get("status") or "never",
            "item_count": r.get("item_count") or 0,
            "error": r.get("error") or "",
        })
    return {
        "last_ok_at": allrow.get("last_ok_at"),
        "last_attempt_at": allrow.get("last_attempt_at"),
        "status": allrow.get("status") or "never",
        "error": allrow.get("error") or "",
        "item_count": allrow.get("item_count") or 0,
        "sources": sources,
        "tracked_sources": _sites() + _kb_keys(),
        "interval_seconds": REFRESH_INTERVAL_SECONDS,
        "stale_seconds": REFRESH_STALE_SECONDS,
        # 2026-10-05（建议3）：顶栏拿「调度上次跑完时刻」与「整轮最后成功时刻」一起判新鲜度，
        # 避免调度在跑、__all__ 却没回写时把用户吓一跳（旧版就误报过「已超时 36 分钟」）。
        "scheduler_last_run_at": _sched_load_state().get("last_run_at"),
        "scheduler_enabled": bool(_sched_load_state().get("enabled", True)),
        "stale_basis": f"{REFRESH_STALE_SECONDS}s（= 刷新周期 {REFRESH_INTERVAL_SECONDS}s × 2）",
        # 2026-10-09：嵌入行情新鲜度（前端 loadRefreshStatus 已读 refresh_status.market）。
        # 仅在旧字段之外**新增**一个 key，向后兼容；用 api_market_freshness 的 2s 原子缓存，
        # 常态下几乎零成本、不拖慢本接口。取不到时 market.ok=False 并带 reason，绝不造假。
        "market": api_market_freshness().get("market"),
        # 2026-10-30：已退役/已冻结的数据源。顶栏常驻提示，避免面板把冻结数据当今天的。
        "retired_feeds": _retired_feeds(),
    }
