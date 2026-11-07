# -*- coding: utf-8 -*-
"""strategies 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import datetime
from viewer.server import (  # noqa: E402,F401
    DB,
    _STRATEGY_SWITCH_FAMILIES,
    _STRATEGY_SWITCH_SCHOOL,
    _STRATEGY_WINDOWS,
    _load_json_or_none,
    _strategy_side_window_obj,
    _strategy_status_note,
    _strategy_switch_kind,
    _strategy_window_obj,
)

def api_strategies() -> dict:
    """GET /api/strategies —— 策略实验室一次全量数据（设计文档 §2 决策 6）。"""
    # 1) 策略目录（唯一真源）；导入失败也不 500，降级为空目录。
    try:
        import strategy_data  # noqa: PLC0415 —— 惰性导入，避免循环依赖

        catalog = list(getattr(strategy_data, "STRATEGIES", []) or [])
    except Exception:  # noqa: BLE001
        catalog = []

    # 2) 窗口指标（唯一真源）；表缺失 → 全部按「无数据」处理（优雅降级）。
    win_rows: dict = {}
    try:
        for r in DB.conn.execute("SELECT * FROM strategy_sim_window").fetchall():
            win_rows[(r["strategy_key"], r["window_label"])] = r
    except Exception:  # noqa: BLE001
        win_rows = {}

    # 3) meta / caliber；表缺失时给默认口径横幅。
    meta: dict = {}
    try:
        mrow = DB.conn.execute("SELECT * FROM strategy_sim_meta WHERE id=1").fetchone()
        if mrow is not None:
            meta = {k: mrow[k] for k in mrow.keys()}
    except Exception:  # noqa: BLE001
        meta = {}

    caliber = _load_json_or_none(meta.get("caliber_json")) if meta else None
    if not isinstance(caliber, dict):
        caliber = {
            "title": "回测口径",
            "text": ("等权单标的满仓、信号次日收盘成交、不计手续费与滑点；"
                     "用于策略相对强弱比较，不是可实现收益预测。"),
            "fill": "next_close",
            "limit_up_filter": True,
            "bench": {"code": "000001", "name": "上证指数"},
            "bars_fetch": int(meta.get("bars_fetch") or _STRATEGY_WINDOWS[0]["bars"]),
        }
    # 2026-09-26 补充：年化口径为 244 交易日/年（引擎 _annualize 依据，界面须标注）。
    caliber["annual_basis"] = "244 交易日/年"

    try:
        import strategy_sim  # noqa: PLC0415

        min_trades = int(getattr(strategy_sim, "MIN_TRADES", 30))
    except Exception:  # noqa: BLE001
        min_trades = 30

    n_not_bt = 0
    n_scope = 0
    n_ranked = 0
    schools: list = []
    strategies: list = []

    for st in catalog:
        key = st.get("key")
        school = st.get("school")
        family = st.get("family")
        if school and school not in schools:
            schools.append(school)

        backtestable = bool(st.get("backtestable", True))
        scope_limited = bool(st.get("scope_limited"))
        if not backtestable:
            n_not_bt += 1
            status = "not_backtestable"
        elif scope_limited:
            n_scope += 1
            status = "scope_limited"
        else:
            status = "ok"

        is_switch = (school == _STRATEGY_SWITCH_SCHOOL) or (family in _STRATEGY_SWITCH_FAMILIES)
        item = {
            "key": key,
            "name": st.get("name"),
            "school": school,
            "family": family,
            "desc": st.get("desc"),
            "buy": st.get("buy"),
            "sell": st.get("sell"),
            "risk": st.get("risk"),
            "source": st.get("source"),
            "params": st.get("params"),
            "backtestable": backtestable,
            "needs": st.get("needs"),
            "scope_limited": scope_limited,
            "status": status,
            "status_note": None if status == "ok" else _strategy_status_note(st),
            "is_timing_switch": is_switch,
            "switch_kind": _strategy_switch_kind(family, school) if is_switch else None,
            "mid": None,
            "windows": None,
        }

        if status == "ok":
            mid_row = win_rows.get((key, "mid"))
            item["mid"] = _strategy_window_obj(mid_row)
            if mid_row is not None:
                n_ranked += 1
            side = [
                _strategy_side_window_obj(win_rows.get((key, "h1")), "近半年"),
                _strategy_side_window_obj(win_rows.get((key, "y1")), "近一年"),
            ]
            # 两窗口均无数据 → 整体置 None（前端按空态处理，不用 0 冒充）。
            item["windows"] = side if any(x is not None for x in side) else None

        strategies.append(item)

    return {
        "generated_at": meta.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "caliber": caliber,
        "meta": {
            "total": len(catalog),
            "backtestable": len(catalog) - n_not_bt,
            "not_backtestable": n_not_bt,
            "scope_limited": n_scope,
            "ranked": n_ranked,
            "min_trades": min_trades,
            "windows": _STRATEGY_WINDOWS,
            "schools": schools,
            "board_note": ("标的池含沪深主板/创业板/科创板/北交所；"
                           "涨跌停阈值按板块（主板10%/创科20%/北交30%/ST5%）。"),
        },
        "strategies": strategies,
    }
