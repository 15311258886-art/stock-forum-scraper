# -*- coding: utf-8 -*-
"""fund 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging
from viewer.server import (  # noqa: E402,F401
    DB,
    _fund_strat_brief,
    _load_json_or_none,
    _ro_conn,
    record_source_error,
)

def api_fund_strategies(key: str | None) -> dict:
    """GET /api/fund_strategies?key=XXX —— 某策略对应的公募基金/ETF 关联（净口径）。

    读 fund_strategy_signal / fund_strategy_meta（引擎 fund_link.py 写，接口只读）。
    来源逐条在每行 fund 的 `source` 字段标注；表缺失 / 未生成 → 优雅降级提示。
    """
    # 1) 策略目录真源
    strat = None
    try:
        import strategy_data  # noqa: PLC0415
        for s in getattr(strategy_data, "STRATEGIES", []) or []:
            if s.get("key") == key:
                strat = s
                break
    except Exception:  # noqa: BLE001
        strat = None

    # 2) 关联数据是否已生成（表缺失 → 优雅降级）
    try:
        mrow = DB.conn.execute("SELECT * FROM fund_strategy_meta WHERE id=1").fetchone()
    except Exception:  # noqa: BLE001
        mrow = None
    if mrow is None:
        return {
            "available": False,
            "strategy": _fund_strat_brief(strat, key),
            "message": ("尚未生成基金关联数据。请在项目根目录运行 `python fund_link.py` "
                        "（需联网抓取东方财富基金净值）后重试。"),
        }
    meta = {k: mrow[k] for k in mrow.keys()}
    coverage = _load_json_or_none(meta.get("coverage_json")) or {}
    sources = _load_json_or_none(meta.get("sources_json")) or []
    cov = coverage.get(key)

    # 3) 该策略命中基金（按 距今天数↑、信号日至今收益↓ 排序，避免 SQL 方言差异）
    funds: list = []
    try:
        rows = _ro_conn().execute(
            "SELECT fund_code,fund_name,fund_type,market,signal_date,days_ago,ret_since,ret_20d,"
            "ret_120d,ret_1y,nav_latest,nav_date,bars,caliber,caliber_note,source,computed_at "
            "FROM fund_strategy_signal WHERE strategy_key=?", (key,)
        ).fetchall()
        for r in rows:
            funds.append(dict(r))
        funds.sort(key=lambda d: (d.get("days_ago") is None, d.get("days_ago") or 0,
                                  -(d.get("ret_since") or -1e9)))
    except Exception as e:  # noqa: BLE001 —— P2-03：源错误记录日志 + 上抛前端，不再静默吞
        logging.exception("fund_strategy_signal 查询失败（退化为空列表）")
        record_source_error("fund_strategy_signal", e)
        funds = []

    # 4) 可判定性 / 口径
    if cov is None:
        judge = {"status": "unknown", "reason": "该策略未纳入净口径判定范围"}
    else:
        judge = {"status": cov.get("status"), "caliber": cov.get("caliber"),
                 "hits": cov.get("hits", 0), "reason": cov.get("reason"),
                 "scope": cov.get("scope")}

    # 5) 每只基金被多少条策略共同命中（共识度）—— 一次聚合，前端可按此排序/分级
    strat_counts: dict = {}
    try:
        for r in DB.conn.execute(
            "SELECT fund_code, COUNT(DISTINCT strategy_key) AS n "
            "FROM fund_strategy_signal GROUP BY fund_code"
        ):
            strat_counts[r["fund_code"]] = r["n"]
    except Exception:  # noqa: BLE001
        pass
    total_strategies = 0
    try:
        total_strategies = DB.conn.execute(
            "SELECT COUNT(DISTINCT strategy_key) FROM fund_strategy_signal"
        ).fetchone()[0]
    except Exception:  # noqa: BLE001
        total_strategies = 0
    for f in funds:
        f["n_strategies"] = strat_counts.get(f["fund_code"], 0)

    return {
        "available": True,
        "strategy": _fund_strat_brief(strat, key),
        "generated_at": meta.get("generated_at"),
        "pool_note": meta.get("pool_note"),
        "nav_basis": meta.get("nav_basis"),
        "nav_end": meta.get("nav_end"),
        "nav_bars": meta.get("nav_bars"),
        "sources": sources,
        "judge": judge,
        "funds": funds,
        "fund_count": len(funds),
        "n_strategies_total": total_strategies,
    }
