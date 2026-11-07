# -*- coding: utf-8 -*-
"""watchdesk 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import threading
from viewer.server import (  # noqa: E402,F401
    DB,
    _WATCHDOG_STALE_SECONDS,
    _desk_next_at,
    _desk_run_bg,
    _desk_stale_seconds,
    _desk_state,
    _desk_state_update,
    watchdesk,
)

def api_watchdesk() -> dict:
    """盯盘总览。首屏尚未跑过扫描（state 空）时，用结论表里最后一次重算时间兜底，
    否则面板头会显示「上次扫描 —」，看起来像没干活；池子大小也直接取实际行数。"""
    d = watchdesk.list_desk(DB.conn, limit=200)
    s = _desk_state()
    rows_n = len(d.get("rows") or [])
    last = s.get("last_scan_at") or ""
    if not last:
        try:
            r = DB.conn.execute("SELECT MAX(scan_at) FROM stock_desk").fetchone()
            last = (r[0] if r and r[0] else "") or ""
        except Exception:  # noqa: BLE001
            last = ""
    st = d.get("stats")
    if isinstance(st, dict):
        st.update({
            "running": bool(s.get("running")),
            "last_scan_at": last,
            "next_scan_at": _desk_next_at(s.get("last_scan_at")) or _desk_next_at(last),
            "started_at": s.get("started_at") or "",
            "last_stat": s.get("last_stat") or {},
            "pool": (s.get("last_stat") or {}).get("scanned") or rows_n,
            # 2026-10-05（建议6）：把「假活」显式给前端，状态牌不用再猜
            "zombie": bool(s.get("zombie_at") or (not s.get("running") and _desk_stale_seconds() > _WATCHDOG_STALE_SECONDS)),
            "zombie_at": s.get("zombie_at") or "",
            "zombie_scan_age": int(_desk_stale_seconds()),
            "heartbeat_stale_seconds": _WATCHDOG_STALE_SECONDS,
        })
    return d
def api_watchdesk_detail(code: str) -> dict:
    return watchdesk.detail(DB.conn, code, window=watchdesk._WINDOW_DAYS)
def api_watchdesk_scan() -> dict:
    """前端「立即扫描」：不到冷却时间也放行（手动优先），后台线程跑，立刻返回。"""
    _desk_state_update(last_scan_at="")       # 清掉冷却起点，让本轮立刻触发
    threading.Thread(target=_desk_run_bg, name="watchdesk-scan-now", daemon=True).start()
    return {"ok": True, "accepted": True}
