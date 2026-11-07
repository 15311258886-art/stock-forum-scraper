# -*- coding: utf-8 -*-
"""lowbox 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import datetime
from viewer.server import (  # noqa: E402,F401
    _lowbox_bo_state,
    _lowbox_break_log_append,
    _lowbox_break_log_load,
    _lowbox_scan_cache_load,
    _lowbox_watch_push,
    api_kline,
)

def api_lowbox_breakouts() -> dict:
    """GET /api/lowbox_breakouts —— 突破雷达：复评最近一次「长期低位」扫描池的突破状态。

    口径（达瓦斯本土化 + 战法库 box_breakout_retest）：
      · 突破 = 收盘站上箱顶 且 当日量 ≥ 箱体日均量 × 1.5（原著参数）；
      · 回踩确认 = 近 15 日曾触发突破、现价回落至箱顶 ±3% 内（二次确认，降假突破）。
    突破/回踩触发 → 落事件日志 + 推关注池未读提醒（同票同规则当日/未读去重）。
    """
    snap = _lowbox_scan_cache_load()
    rows0 = snap.get("rows") or []
    if not rows0:
        return {"ok": False, "reason": "还没有扫描过：请先在「长期低位」跑一次扫描",
                "rows": [], "events": [], "pushed_n": 0}
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    log = _lowbox_break_log_load()
    logged = {(e.get("code"), e.get("kind"), str(e.get("date") or "")[:10]) for e in log}
    last_break = {}
    for e in log:
        if e.get("kind") == "breakout":
            last_break[e.get("code")] = str(e.get("date") or "")[:10]

    # 2026-10-29：突破雷达要「最近 130 根日线」，原来逐只调 api_kline（80 只 → 最多 80 次联网，
    # 冷态实测 12.5 秒）。本地仓一次批量取完（1 条 SQL），口径同源、零网络。
    _bo_kl = {}
    try:
        from viewer import localfeed as _lf
        _bo_kl = _lf.bars_map([r.get("code") for r in rows0[:80] if r.get("code")], keep_bars=130)
    except Exception:  # noqa: BLE001  本地仓不可用 → 退回逐只 api_kline
        _bo_kl = {}

    def _one(r):
        code = r.get("code")
        try:
            local = _bo_kl.get(code) or []
            if local:
                kl = {"klines": [{"date": d, "close": c, "high": h, "low": lo, "volume": v}
                                 for (d, c, h, lo, v) in local]}
            else:
                kl = api_kline(code, "day", limit=130)
            if not kl or kl.get("error") or not kl.get("klines"):
                return None
            k = kl["klines"][-1]
            cur = float(k["close"]) if k.get("close") is not None else None
            vol = float(k["volume"]) if k.get("volume") is not None else None
            box_high = r.get("box_high")
            avg_vol = r.get("box_avg_vol")
            vr = round(vol / avg_vol, 2) if (vol and avg_vol) else None
            state = _lowbox_bo_state(cur, box_high, vr)
            # 回踩确认：近15日突破过 + 现价回到箱顶±3%
            if state == "in_box" and box_high and cur and cur >= box_high * 0.97:
                d0 = last_break.get(code)
                if d0:
                    try:
                        if 0 <= (now.date() - datetime.fromisoformat(d0).date()).days <= 15:
                            state = "retest"
                    except ValueError:
                        pass
            return {"code": code, "name": r.get("name") or "", "cur": cur,
                    "box_high": box_high, "vol_ratio": vr, "state": state,
                    "breakout_pct": round((cur / box_high - 1.0) * 100.0, 2)
                    if (cur and box_high) else None}
        except Exception:  # noqa: BLE001
            return None

    from concurrent.futures import ThreadPoolExecutor
    out, pushed = [], 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(_one, rows0[:80]):
            if not res:
                continue
            out.append(res)
            if res["state"] in ("breakout", "retest"):
                kind = res["state"]
                if (res["code"], kind, today) in logged:
                    continue
                ev = {"code": res["code"], "name": res["name"], "kind": kind,
                      "date": today, "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                      "price": res["cur"], "box_high": res["box_high"],
                      "vol_ratio": res["vol_ratio"]}
                _lowbox_break_log_append(ev)
                logged.add((res["code"], kind, today))
                if _lowbox_watch_push(res["code"], res["name"],
                                      "box_breakout" if kind == "breakout" else "box_retest",
                                      res["box_high"], res["cur"]):
                    pushed += 1
    out.sort(key=lambda x: (0 if x["state"] in ("breakout", "retest") else
                            1 if x["state"] in ("stand_top", "near") else 2, -(x.get("cur") or 0)))
    return {"ok": True, "ts": now.strftime("%Y-%m-%d %H:%M:%S"), "rows": out,
            "events": log[-30:], "pushed_n": pushed}
