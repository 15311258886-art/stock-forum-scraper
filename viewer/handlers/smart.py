# -*- coding: utf-8 -*-
"""smart 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging, time
from viewer.server import (  # noqa: E402,F401
    DB,
    _mainline_map,
    _sector_reverse,
)

def api_smart_money(min_holders: int = 2, days: int = 5,
                     max_age_days: int | None = None) -> dict:
    """「别人怎么做」置顶「聪明钱共识榜」（P0-E）：6 个名单 → 合成 1 条结论。
    口径（沿用 2026-10-10 批准的信号拆分）：
      共识 = 当前持仓快照里**喊买入**的人数 buy_holders（不掺 HOLD/WATCH，避免"关注"虚增看多）；
      近期 = 近 N 日**同向买入**（动作发生时点）。两者互补，分别标注，不混成一个数字。"""
    log = logging.getLogger(__name__)
    out: dict = {"ok": True, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "min_holders": min_holders, "days": days,
                 "consensus": [], "recent": [], "directions": [], "mainline_date": None,
                 "basis": ""}
    mdate, mmap = _mainline_map()
    out["mainline_date"] = mdate

    try:
        cons = DB.consensus_stocks(min_holders=min_holders,
                                   max_age_days=max_age_days) or []
        cons.sort(key=lambda x: (-(x.get("buy_holders") or 0), -(x.get("holders") or 0)))
        # 2026-10-15 P0-1：把 consensus 行携带的 data_asof 回带顶层，供前端角标。
        out["data_asof"] = max((str(x.get("data_asof") or "") for x in cons
                                if x.get("data_asof")), default="")
        out["positions_max_age_days"] = next(
            (x.get("positions_max_age_days") for x in cons if x.get("data_asof")), None)
        for x in cons[:12]:
            c = str(x.get("symbol") or "")
            ml = mmap.get(c) or {}
            out["consensus"].append({
                "symbol": c, "name": x.get("symbol_name") or "",
                "buy_holders": x.get("buy_holders") or 0,
                "hold_holders": x.get("hold_holders") or 0,
                "watch_holders": x.get("watch_holders") or 0,
                "holders": x.get("holders") or 0,
                "sector": ml.get("sector") or "", "pct": ml.get("pct"),
                "forward": bool(ml.get("forward")),
            })
    except Exception as e:  # noqa: BLE001
        log.warning("smart_money 共识失败: %s", e)

    try:
        co = DB.cohold_recent(days=days, min_holders=min_holders) or {}
        for x in (co.get("buy") or [])[:10]:
            c = str(x.get("symbol") or "")
            ml = mmap.get(c) or {}
            out["recent"].append({
                "symbol": c, "name": x.get("symbol_name") or "",
                "holders": x.get("holders") or len(x.get("players") or []),
                "buy_time": str(x.get("buy_time") or "")[:16],
                "sector": ml.get("sector") or "", "pct": ml.get("pct"),
            })
    except Exception as e:  # noqa: BLE001
        log.warning("smart_money 近期共买失败: %s", e)

    # 方向汇总：把共识个股按「概念板块」归堆（回答"多少人的共识票扎堆在同一方向"）。
    # 先用概念反向表（概念 → 代码）求每只票的概念，再只保留 **≥2 只共识票重合** 的概念；
    # 一个都没有时退回用今日主线方向标注（数据日见 mainline_date），不硬凑一个方向出来。
    c2s: dict = {}
    try:
        for concept, codes in (_sector_reverse() or {}).items():
            for c in codes:
                c2s.setdefault(str(c), []).append(concept)
    except Exception as e:  # noqa: BLE001
        log.warning("smart_money 概念反向表失败: %s", e)

    def _group(get_sector) -> dict:
        dm: dict = {}
        for x in out["consensus"]:
            for sec in (get_sector(x) or []):
                e = dm.setdefault(sec, {"sector": sec, "n": 0, "buy_holders": 0, "stocks": []})
                e["n"] += 1
                e["buy_holders"] += x["buy_holders"]
                e["stocks"].append({"symbol": x["symbol"], "name": x["name"],
                                    "buy_holders": x["buy_holders"]})
        return dm

    dirs = [v for v in _group(lambda x: (c2s.get(x["symbol"]) or [])[:8]).values() if v["n"] >= 2]
    if not dirs:
        dirs = list(_group(lambda x: [x["sector"] or "（不在今日主线内）"]).values())
    out["directions"] = sorted(dirs, key=lambda x: (-x["n"], -x["buy_holders"]))[:6]

    out["basis"] = ("口径：共识榜按「喊买入的人数」排序（buy_holders，来自 positions 快照 + 选手层级加权，"
                    "已剔除机器号与 HOLD/WATCH 干扰）；近期共买看的是近 %d 日**动作时点**（≥%d 人同向买入）。"
                    "方向归属取今日主线（数据日 %s）。它回答的是「别人在买什么」，不是买入建议。"
                    % (days, min_holders, mdate or "—"))
    return out
