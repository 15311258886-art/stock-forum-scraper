# -*- coding: utf-8 -*-
"""dashboard 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging, time
from viewer.server import (  # noqa: E402,F401
    DB,
    _api_cache_get,
    _api_cache_get_any,
    _api_cache_put,
    _dashboard_snapshot_load,
    _dashboard_snapshot_save,
    _jl,
    _num,
    _swr_refresh,
    api_niusan,
)

def api_dashboard(force=False) -> dict:
    """今日看板：一屏给出「今天什么值得看」——主线研判（方向/龙头/梯队/分支） → 共识 → 数据新鲜度。
    组织维度是「判断」而非「数据来源」，作为产品的决策入口（首页）。

    2026-10-25（进哥：不能一句「超时」就把界面晾那儿）：冷重算实测 32s，超过前端 30s 预算，
    于是每次缓存失效都必然报超时。改为 stale-while-revalidate —— 过期就先把上一份结果顶上去
    （前端据 _stale/_cache_age 说明时效），后台线程重算；重启后无内存缓存时读盘上快照。
    """
    if not force:
        cached = _api_cache_get("dashboard")
        if cached is not None:
            return cached
        snapshot, age = _api_cache_get_any("dashboard")
        if not isinstance(snapshot, dict):
            snapshot, age = _dashboard_snapshot_load()   # 刚重启：内存空，读盘
        if isinstance(snapshot, dict):
            _swr_refresh("dashboard", lambda: api_dashboard(force=True))
            snap = dict(snapshot)
            snap["_stale"] = True
            snap["_cache_age"] = int(age or 0)
            return snap
    _t0 = time.time()
    _marks: list = []

    def _mark(tag: str) -> None:
        _marks.append((tag, round(time.time() - _t0, 2)))

    out: dict = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # ---- 1) 龙虎榜：最新交易日 ----
    row = DB.conn.execute("SELECT MAX(trade_date) AS d FROM lhb_stocks").fetchone()
    lhb_date = row["d"] if row else None
    lhb: dict = {"date": lhb_date, "stock_count": 0, "net_total": 0.0,
                 "top_stocks": [], "inst_top": [], "dept_top": []}
    if lhb_date:
        merged: dict = {}
        for r in DB.conn.execute(
                "SELECT security_code, security_name, change_rate, net_amt, reason "
                "FROM lhb_stocks WHERE trade_date=?", (lhb_date,)).fetchall():
            k = r["security_code"]
            m = merged.get(k)
            if m is None:
                merged[k] = {"symbol": k, "name": r["security_name"],
                             "change_rate": _num(r["change_rate"]),
                             "net_amt": _num(r["net_amt"]) or 0.0,
                             "reasons": [r["reason"]] if r["reason"] else []}
            else:
                m["net_amt"] += _num(r["net_amt"]) or 0.0
                if r["reason"] and r["reason"] not in m["reasons"]:
                    m["reasons"].append(r["reason"])
        rows = sorted(merged.values(), key=lambda x: -x["net_amt"])
        lhb["stock_count"] = len(rows)
        lhb["net_total"] = round(sum(x["net_amt"] for x in rows), 2)
        lhb["top_stocks"] = rows[:8]
        lhb["inst_top"] = [
            {"symbol": r["security_code"], "name": r["security_name"],
             "net": _num(r["net_buy_amt"]), "buy_times": r["buy_times"],
             "sell_times": r["sell_times"], "ratio": _num(r["ratio"])}
            for r in DB.conn.execute(
                "SELECT security_code, security_name, net_buy_amt, buy_times, sell_times, ratio "
                "FROM lhb_inst WHERE trade_date=? AND net_buy_amt>0 "
                "ORDER BY net_buy_amt DESC LIMIT 6", (lhb_date,)).fetchall()]
        lhb["dept_top"] = [
            {"name": (r["org_name"] or r["operate_name"]), "net": _num(r["net"]),
             "stock_count": r["stk"], "appear": r["n"]}
            for r in DB.conn.execute(
                "SELECT operate_name, org_name, SUM(net_amt) AS net, "
                "COUNT(DISTINCT security_code) AS stk, COUNT(*) AS n "
                "FROM lhb_depts WHERE trade_date=? GROUP BY operate_name "
                "ORDER BY net DESC LIMIT 6", (lhb_date,)).fetchall()]
    out["lhb"] = lhb
    _mark("龙虎榜")

    # ---- 2) 板块动向：最新日的多点共振 ----
    srow = DB.conn.execute("SELECT MAX(trade_date) AS d FROM lhb_sector_focus").fetchone()
    sdate = srow["d"] if srow else None
    sectors = []
    if sdate:
        for r in DB.conn.execute(
                "SELECT sector, net_amt, onlist_n, t1_avg, t3_avg, t1_win, bench_t1, bench_t3 "
                "FROM lhb_sector_focus WHERE trade_date=? "
                "ORDER BY net_amt DESC LIMIT 6", (sdate,)).fetchall():
            t1 = _num(r["t1_avg"])
            b1 = _num(r["bench_t1"])
            sectors.append({"sector": r["sector"], "net": _num(r["net_amt"]),
                            "onlist_n": r["onlist_n"], "t1_avg": t1, "t3_avg": _num(r["t3_avg"]),
                            "t1_win": _num(r["t1_win"]), "bench_t1": b1,
                            "excess_t1": (round(t1 - b1, 2) if t1 is not None and b1 is not None else None)})
    out["sectors"] = {"date": sdate, "rows": sectors}
    _mark("板块动向")

    # ---- 2.5) 主线研判：方向 → 龙头 → 梯队 → 分支（决策口径，不做个股罗列） ----
    # 打分口径（可解释，全部来自真实数据）：
    #   资金强度 40（该方向净买 / 当日最强方向净买）
    # + 龙头强度 30（龙头当日涨幅：涨停 30 / ≥5% 20 / 其余 10）
    # + 共振广度 20（该方向上榜个股数 × 1.2，上限 20）
    # + 分支延伸 10（关联概念数 × 2，上限 10）
    # 评级：≥75 重点方向 / ≥55 跟进关注 / 其余 仅观察。
    # 「可推进」= 梯队中尚未涨停（<9.8%）的同方向个股——龙头已封板，能动的在这批里。
    mainlines: list[dict] = []
    cdate = None
    try:
        crow = DB.conn.execute("SELECT MAX(trade_date) AS d FROM sector_chain").fetchone()
        cdate = crow["d"] if crow else None
        if cdate:
            raw = DB.conn.execute(
                "SELECT sector, net_amt, onlist_n, leader_code, leader_name, leader_net, "
                "leader_chg, leader_pct_now, onlist_json, tiers_json, branches_json, "
                "t1_avg, t3_avg FROM sector_chain WHERE trade_date=? "
                "ORDER BY net_amt DESC", (cdate,)).fetchall()
            top_net = max((_num(r["net_amt"]) or 0.0) for r in raw) if raw else 0.0
            for r in raw:
                net = _num(r["net_amt"]) or 0.0
                lead_pct = _num(r["leader_pct_now"])
                n_on = r["onlist_n"] or 0
                onlist = _jl(r["onlist_json"])
                tiers = _jl(r["tiers_json"])
                branches = _jl(r["branches_json"])
                s_cap = 40.0 * (net / top_net) if top_net else 0.0
                lp = lead_pct or 0.0
                s_lead = 30.0 if lp >= 9.8 else (20.0 if lp >= 5.0 else 10.0)
                s_res = min(20.0, n_on * 1.2)
                s_br = min(10.0, len(branches) * 2.0)
                score = round(s_cap + s_lead + s_res + s_br, 1)
                if score >= 75:
                    rating, level = "重点方向", "hot"
                elif score >= 55:
                    rating, level = "跟进关注", "warm"
                else:
                    rating, level = "仅观察", "cool"
                forward = [t for t in tiers if (_num(t.get("pct")) or 0) < 9.8][:6]
                why = ("资金居前" if s_cap >= 30 else "资金中游") \
                    + "·" + ("龙头封板" if lp >= 9.8 else ("龙头走强" if lp >= 5 else "龙头偏弱")) \
                    + "·" + ("多点共振(%d家上榜)" % n_on if n_on >= 3 else "个股孤立") \
                    + ("·分支多(%d)" % len(branches) if len(branches) >= 4 else "")
                mainlines.append({
                    "sector": r["sector"], "net": net, "net_yi": round(net / 1e8, 2),
                    "onlist_n": n_on, "onlist": onlist[:10],
                    "leader_code": r["leader_code"], "leader_name": r["leader_name"],
                    "leader_net_yi": round((_num(r["leader_net"]) or 0.0) / 1e8, 2),
                    "leader_chg": _num(r["leader_chg"]), "leader_pct_now": lead_pct,
                    "tiers": tiers[:10], "forward": forward,
                    "branches": branches[:8],
                    "t1_avg": _num(r["t1_avg"]), "t3_avg": _num(r["t3_avg"]),
                    "score": score, "rating": rating, "level": level, "why": why,
                    "cap_pct": round(s_cap / 40.0 * 100, 0) if top_net else 0,
                })
    except Exception as e:  # noqa: BLE001 —— 看板不应因单个模块失败而整体不可用
        logging.debug("主线研判构建失败：%s", e)
    out["mainlines"] = {"date": cdate, "rows": mainlines,
                        "focus": [m for m in mainlines if m["level"] in ("hot", "warm")][:3]}

    # ---- 3) 共识：牛散同持 ----
    try:
        ni = api_niusan(min_hold=2, scope="free")
        sm = ni.get("summary") or {}
        out["consensus"] = {"star_count": sm.get("investor_total"),
                            "person_total": sm.get("person_total"),
                            "latest_date": sm.get("latest_date"),
                            "rows": (ni.get("cohold") or [])[:6]}
    except Exception:  # noqa: BLE001 —— 看板不应因单个模块失败而整体不可用
        out["consensus"] = {"star_count": None, "rows": []}

    _mark("共识/牛散")

    # ---- 4) 数据新鲜度（一眼看出哪条数据陈旧） ----
    # 口径说明：持仓（当前持仓）源站不提供建仓时间，故此处不展示"最新持仓日"
    # （否则会拿少数带日期的社区帖冒充整体，造成误导）；改展示条数 + 最后抓取时间。
    fresh = []
    for label, sql in (
        ("龙虎榜", "SELECT MAX(trade_date) FROM lhb_stocks"),
        ("实盘交易", "SELECT MAX(post_time) FROM trades"),
        ("讨论帖", "SELECT MAX(post_time) FROM posts"),
        ("股东(季报)", "SELECT MAX(end_date) FROM stock_holders"),
        ("最后抓取", "SELECT MAX(fetched_at) FROM raw_pages"),
    ):
        try:
            v = DB.conn.execute(sql).fetchone()[0]
        except Exception:  # noqa: BLE001
            v = None
        fresh.append({"label": label, "latest": str(v)[:19] if v else "—"})
    try:
        fresh.append({"label": "持仓条数", "latest": str(
            DB.conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]) + " 条"})
    except Exception:  # noqa: BLE001
        pass
    out["freshness"] = fresh
    _mark("新鲜度")
    # 2026-10-01 集合竞价（方案 A）：今日总览第一屏 —— 开盘前先看「今天一开盘市场什么情绪」，
    # 开盘后再看当天竞价结果。随 dashboard 一起下发，前端不额外发请求（dashboard 本来就必拉）。
    try:
        import auction as _auc
        # 2026-10-25（进哥：看板必须秒开）：集合竞价内部要联网取行情（_http timeout=20），
        # 分段实测它单独吃掉 10.29s / 共 10.3s —— 即 99% 的时间都耗在这一段，32s 那次也是它。
        # 改为独立缓存 + SWR：过期先用上次结果并在后台刷新，看板永不为它排队。
        _auc_val = _api_cache_get("auction")
        if _auc_val is None:
            _auc_old, _auc_age = _api_cache_get_any("auction")
            if isinstance(_auc_old, dict):
                _auc_val = _auc_old
                out.setdefault("_degraded", []).append(
                    "集合竞价：显示 %d 秒前快照，后台刷新中" % int(_auc_age or 0))
                _swr_refresh("auction", lambda: _api_cache_put("auction", _auc.summary(), 300))
            else:
                _auc_val = _auc.summary()
                _api_cache_put("auction", _auc_val, 300)
        out["auction"] = _auc_val
    except Exception as e:  # noqa: BLE001 —— 这一块挂了不能拖垮整个看板
        out["auction"] = {"status": "error", "date": None, "counts": {}, "top_pct": [],
                          "top_amount": [], "top_ratio": [], "history": [],
                          "note": "竞价板块出错：%s" % str(e)[:120],
                          "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _mark("集合竞价")
    _api_cache_put("dashboard", out, 300)
    _dashboard_snapshot_save(out)
    _cost = time.time() - _t0
    if _cost >= 3:
        logging.getLogger(__name__).warning(
            "dashboard 冷重算 %.1fs｜分段 %s",
            _cost, ", ".join("%s %.2fs" % (t, s) for t, s in _marks))
    return out
