# -*- coding: utf-8 -*-
"""screener 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging, os, re, time
from viewer.server import (  # noqa: E402,F401
    CFG,
    DB,
    _BOTTOM_TIERS,
    _DECLINE_TTL,
    _actual_holder_tag,
    _bottom_cache_load,
    _bottom_scan_async,
    _decline_pool,
    _dividend_ttm,
    _fund_metrics,
    _fund_score,
    _long_worth,
    _lowbox_bo_state,
    _lowbox_metrics,
    _lowbox_prefetch_external,
    _lowbox_scan_cache_save,
    _org_profile,
    _pc,
    _quality_pass,
    _state_backing_tag,
    _valuation_cached,
    api_kline,
    board_flow,
    screener,
    watchlib,
)

def api_screener(screen: str = "value", limit: int = 40, params: dict = None) -> dict:
    """智能选股：screen=逗号分隔指标 key，params={key:阈值}（前端可调阈值）。"""
    return screener.run(screen=screen, limit=limit, params=params)
def api_screener_boards(kind: str = "industry", pz: int = 90) -> dict:
    """板块清单（新浪主源/东财兜底），供「板块分类」入口。"""
    return board_flow.board_list(kind=kind, pz=pz)
def api_screener_board_members(code: str = "") -> dict:
    """板块成分股 + 综合分排序（市值50%+成交额30%+换手率20%，知名度口径）。"""
    # 板块代码形如 new_ljhy（含下划线），只放行字母/数字/下划线——别把下划线剥掉
    code = "".join(ch for ch in str(code or "") if ch.isalnum() or ch == "_")[:24]
    return board_flow.members_rich(code)
def api_screener_decline(days: int = 30, down: int = 25, limit: int = 80,
                         sort: str = "down", force: int = 0) -> dict:
    """连续阴跌扫描（活跃池内，近 days 个交易日中下跌 >= down 天）。
    sort="score" 时按换手率策略「入手值得率」降序（2026-09-30，王总需求）。

    扫描要过活跃池 + 逐票日线，重复点同一组参数不该重算：**按参数键内存缓存 1 小时**，
    同样参数cache 命中就在载荷里标 `cached=True`（前端可据此省掉转圈）；`force=1` 强制重算。
    """
    key = (int(days), int(down), int(limit), sort or "down")
    now = time.time()
    cached = _DECLINE_CACHE.get(key)
    if cached and now - cached[0] < _DECLINE_TTL and not force:
        return dict(cached[1], cached=True)
    res = screener.decline_scan(_decline_pool(), days=days, down=down, limit=limit, sort=sort)
    _DECLINE_CACHE[key] = (now, res)
    return res
def api_screener_turnover_top(days: int = 30, limit: int = 30) -> dict:
    """GET /api/screener_turnover_top —— 全市场换手率实战 Top N（按入手值得率降序，
    2026-09-30 王总需求：阴跌板块一键筛大盘前30）。"""
    return screener.turnover_top(days=days, limit=limit)
def api_screener_lowbox(codes: str = "", limit: int = 30, box_window: int = 750,
                        low_pct: float = 30.0, quality: str = "off",
                        w_shape: float = 40.0, w_fund: float = 35.0,
                        w_worth: float = 25.0) -> dict:
    """GET /api/screener_lowbox —— 板块二：长期低位震荡 + 基本面 + 国资系，一次性全量版。

    codes 为空 → 关注池 ∪ 板块一Top30；否则用逗号分隔的自定义代码（自定义优先、去重）。
    逐只复用 api_kline(limit=0) 拉全历史日K（90s 缓存），并发线程池；首跑需补K线，二次秒出。
    2026-10-13 进哥「一次性都做进去」：
      · 基本面（增强期并入）：估值五因子(valuation.score) + ROE/负债率/毛利率/扣非(F10 财务)
        + TTM 股息率(分红明细自算) + 实控人国资打标(公司概况 ACTUAL_HOLDER)；
      · 长窗 worth：250/500/750 三档 30:40:30 加权（直接跑全历史 K，绕开 139 根缓存上限）；
      · 质量硬过滤 off/loose/strict（数据缺失不误杀，标 ⚠）；
      · 综合分 = 形态(w_shape) + 基本面(w_fund) + 长窗worth(w_worth) 权重柱（缺项按比例摊给其余柱）
        + 国资系加分 0~15（国家队15/央国企12/险资9/基金机构6/北向4；实控人国资再+5，封顶15）——
        加分制而非权重柱，因 v0.2 明确「股东是加分不是硬门槛」，非国资票不该被一票压死；
      · 扫描结果落 data/lowbox_scan_cache.json 供 /api/lowbox_breakouts 突破雷达复评。
    """
    import time as _t
    from concurrent.futures import ThreadPoolExecutor
    _t0 = _t.time()
    limit = max(5, min(int(limit or 30), 100))
    # 2026-10-21 进哥：箱体时长要能选 1/2/3/5/8 年与「上市以来」。box_window=0 表示全历史
    # （旧版最小 120 会把 0 夹成 120，导致「上市以来」实际只取半年，是这次一并修掉的坑）。
    _bw = int(box_window or 0)
    box_window = 99999 if _bw <= 0 else max(120, min(_bw, 3000))
    try:
        low_pct = max(5.0, min(float(low_pct or 30.0), 80.0))
    except (TypeError, ValueError):
        low_pct = 30.0
    try:
        w_shape = max(0.0, float(w_shape)); w_fund = max(0.0, float(w_fund)); w_worth = max(0.0, float(w_worth))
    except (TypeError, ValueError):
        w_shape, w_fund, w_worth = 40.0, 35.0, 25.0
    if w_shape + w_fund + w_worth <= 0:
        w_shape, w_fund, w_worth = 40.0, 35.0, 25.0
    quality = quality if quality in ("off", "loose", "strict") else "off"
    # —— 股票池 ——
    universe = []
    watch_set = set()
    try:
        watchlib.ensure_schema(DB.conn)
        items = watchlib.list_items(DB.conn) or []
        watch_set = {it["symbol"] for it in items}
        universe = list(watch_set)
    except Exception:  # noqa: BLE001
        universe = []
    if codes and codes.strip():
        extra = [c.strip() for c in re.split(r"[\s,，、]+", codes) if c.strip()]
        universe = list(dict.fromkeys(extra + universe))   # 自定义优先，去重保序
    else:
        try:
            top = screener.turnover_top(days=30, limit=30) or {}
            for r in (top.get("rows") or []):
                c = r.get("code")
                if c and c not in universe:
                    universe.append(c)
        except Exception:  # noqa: BLE001
            pass
    # 2026-10-30：封顶改为可配置，并如实披露「池子只数 + 是否被截断」。
    # 默认仍 80：实测 200 只跑 3 分钟仍未算完（估值/实控人/分红是逐只外部接口，
    # 成本线性），而 80 只稳定在 30~60s。覆盖度问题用「披露」解决而不是硬扛耗时 ——
    # 这个模块的池子定义本就是「关注池 ∪ 板块Top30」，不是全市场选股。
    # 想扩大用环境变量 LOWBOX_POOL_MAX（如 150），代价是首跑变慢。
    try:
        _pool_max = max(5, min(int(os.environ.get("LOWBOX_POOL_MAX", "80")), 3000))
    except (TypeError, ValueError):
        _pool_max = 80
    _pool_before = len(universe)
    universe = universe[:_pool_max]
    _pool_capped = _pool_before > len(universe)
    # 2026-10-30：批量取 证监会一级行业（industry_csrc 的"-"前段），供低位池行业上限使用。
    # 缺失归 "未知" 组，同样计入上限，避免"缺失即逃避限制"。
    _ind_map = {}
    try:
        _ind_map = _pc.fetch_industry_map(DB.conn, list(universe))
    except Exception:  # noqa: BLE001
        _ind_map = {}
    if not universe:
        return {"ok": False, "reason": "股票池为空（关注池为空且自定义代码为空）",
                "rows": [], "universe_size": 0, "scanned": 0, "elapsed": 0, "params": {}}
    # 外部 F10/分红先分批预取；任何失败都保留给 _one 的原逐只逻辑兜底。
    try:
        _external_prefetch = _lowbox_prefetch_external(list(universe))
    except Exception:  # noqa: BLE001
        logging.exception("lowbox 批量预取总入口异常，退回原逐只取数")
        _external_prefetch = {"requested": len(universe), "error": "批量预取异常，已退回逐只"}
    # —— 避坑标记（整批实时算，替代原博主提及；见 viewer/avoid.py）——
    avoid_map = {}
    try:
        if avoid_mod is not None:
            avoid_map = avoid_mod.compute(DB.conn, universe)
    except Exception:  # noqa: BLE001
        avoid_map = {}
    # —— 限售解禁（紧邻窗口，见 viewer/lift.py）——
    lift_map = {}
    try:
        if lift_mod is not None:
            lift_map = lift_mod.compute(DB.conn, universe)
    except Exception:  # noqa: BLE001
        lift_map = {}
    # —— 逐只算指标（线程池并发拉全历史K；财务/估值/实控人/分红各自带缓存）——
    # —— 逐只算指标（全历史 K 先一次性从本地仓批量取，缺的才走 api_kline 补网）——
    # 2026-10-29：原来这里每只都调 api_kline(limit=0)，即 80 只票各打一次东财全历史，
    # 10 线程并发实测 45 秒仍不返回（前端早已放弃 + 整站被 GIL 拖死）。
    # 本地 kbase.db 本来就有全历史，一次 SQL 全取回，箱体/分位算的口径完全不变。
    _local_kl = {}
    try:
        from viewer import localfeed as _lf
        _local_kl = _lf.klines_bulk(list(universe))
    except Exception:  # noqa: BLE001  本地仓不可用 → 退回逐只 api_kline
        _local_kl = {}

    def _one(code):
        try:
            kl = {"klines": _local_kl.get(code) or []} if _local_kl.get(code) else api_kline(code, "day", limit=0)
            if not kl or kl.get("error") or not kl.get("klines"):
                return None
            m = _lowbox_metrics(kl["klines"], box_window, low_pct)
            if not m.get("ok"):
                return None
            name = ""
            tag = ""
            try:
                f10 = screener.stock_f10(code) or {}
                name = (f10.get("profile") or {}).get("name") or kl.get("name") or ""
                tag = _state_backing_tag(f10.get("holders") or [])
            except Exception:  # noqa: BLE001
                pass
            fund = _fund_metrics(code)
            org = _org_profile(code)
            div = _dividend_ttm(code)
            val = _valuation_cached(code)
            holder_tag = _actual_holder_tag((org or {}).get("actual_holder"))
            dy = None
            if div and div.get("ttm_div") and m.get("current"):
                dy = round(div["ttm_div"] / m["current"] * 100.0, 2)
            long_worth, lw_parts = _long_worth(code, kl["klines"])
            bo_state = _lowbox_bo_state(m.get("current"), m.get("box_high"), m.get("vol_ratio"))
            return {"code": code, "name": name, "current": m["current"],
                    "pct_all": m["pct_all"], "pct_3y": m["pct_3y"],
                    "box_high": m["box_high"], "box_low": m["box_low"],
                    "box_range_pct": m["box_range_pct"], "cv_pct": m["cv_pct"],
                    "slope_yr_pct": m["slope_yr_pct"], "touches": m["touches"],
                    "box_years": m["box_years"], "n_all": m["n_all"],
                    "box_avg_vol": m.get("box_avg_vol"), "vol_ratio": m.get("vol_ratio"),
                    "breakout_pct": m.get("breakout_pct"), "bo_state": bo_state,
                    "long_worth": long_worth, "lw_parts": lw_parts,
                    "state_tag": tag, "holder_tag": holder_tag,
                    "actual_holder": (org or {}).get("actual_holder") or "",
                    "industry": (org or {}).get("industry") or "",
                    "industry_csrc": (org or {}).get("industry_csrc") or "",
                    "industry1": _ind_map.get(code, "未知"),
                    "pe": (val or {}).get("pe"), "pb": (val or {}).get("pb"),
                    "val_score": (val or {}).get("score"),
                    "roe": (fund or {}).get("roe"), "roe_kf": (fund or {}).get("roe_kf"),
                    "dr": (fund or {}).get("dr"), "gm": (fund or {}).get("gm"),
                    "np_yoy": (fund or {}).get("np_yoy"), "kf_np_yoy": (fund or {}).get("kf_np_yoy"),
                    "report": (fund or {}).get("report") or "", "dy": dy,
                    "in_watch": code in watch_set,
                    "avoid": avoid_map.get(code), "lift": lift_map.get(code)}
        except Exception:  # noqa: BLE001
            return None
    out_rows, scanned = [], 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(_one, universe):
            scanned += 1
            if res:
                out_rows.append(res)
    # —— 综合分 v2：形态/基本面/长窗worth 权重柱（缺项摊派）+ 国资系 0~15 加分 ——
    #   形态 = 低位分位50% + 平稳度30% + 横盘20%；基本面 = 估值50%+ROE30%+负债20%；
    #   长窗worth = 250/500/750 三档 30:40:30。
    for r in out_rows:
        low_score = max(0.0, (low_pct - (r["pct_all"] if r["pct_all"] is not None else 100.0)) / max(1.0, low_pct)) * 100.0
        cv = r["cv_pct"] if r["cv_pct"] is not None else 40.0
        flat_score = max(0.0, (40.0 - cv) / 40.0) * 100.0
        slope = abs(r["slope_yr_pct"]) if r["slope_yr_pct"] is not None else 15.0
        slope_score = max(0.0, (15.0 - slope) / 15.0) * 100.0
        shape = 0.5 * low_score + 0.3 * flat_score + 0.2 * slope_score
        fund_s = _fund_score({"roe": r.get("roe"), "dr": r.get("dr")}, {"score": r.get("val_score")})
        worth_score = r.get("long_worth")
        # 2026-10-13 修复：某维度分数可能缺（如 long_worth 因 K 线不足为 None），
        # 直接 s*w 会 TypeError → 整次扫描 500（默认股票池偶发）。改为"缺项不计入权重柱、该维度得 0 分"（摊派给有数据维度）。
        pillars = []
        if shape is not None:
            pillars.append((shape, w_shape))
        if worth_score is not None:
            pillars.append((worth_score, w_worth))
        if fund_s is not None:
            pillars.append((fund_s, w_fund))
        tw = sum(w for _s, w in pillars)
        base = sum(float(s) * w for s, w in pillars) / tw if tw else 0.0
        bonus = 0.0
        bonus += {"国家队": 15.0, "央国企": 12.0, "险资": 9.0,
                  "基金/机构": 6.0, "北向": 4.0}.get(r.get("state_tag") or "", 0.0)
        if r.get("holder_tag") == "国资实控":
            bonus += 5.0
        bonus = min(bonus, 15.0)
        ok_q, q_bad = _quality_pass({"roe": r.get("roe"), "dr": r.get("dr")}, {"pe": r.get("pe")}, quality)
        r["fund_score"] = fund_s
        r["state_bonus"] = bonus
        r["quality_flag"] = q_bad or ("数据缺" if fund_s is None else "")
        r["score"] = round(min(100.0, base + bonus), 1)
        r["low_score"] = round(shape, 1)
        r["quality_pass"] = ok_q
    if quality != "off":
        out_rows = [r for r in out_rows if r["quality_pass"]]
    out_rows.sort(key=lambda x: -x["score"])
    _lowbox_scan_cache_save(out_rows)   # 供 /api/lowbox_breakouts 突破雷达复评（全量扫描，未截断）
    # 2026-10-30：行业上限。已按综合分降序，贪心取前 limit 只，但任一行若其 证监会一级行业
    # 已达上限则跳过（保留更高分的同行业票），使池子行业不过度集中。
    # 上限来自 viewer/config.py（LOWBOX_MAX_PER_INDUSTRY / LOWBOX_MAX_INDUSTRY_PCT，环境变量可覆盖）。
    _cap_per = CFG.LOWBOX_MAX_PER_INDUSTRY
    _cap_pct = CFG.LOWBOX_MAX_INDUSTRY_PCT
    out_rows, _ind_counts = _pc.apply_industry_cap(
        out_rows, limit, max_per_industry=_cap_per, max_industry_pct=_cap_pct,
        industry_key="industry1")
    _cap_top1, _cap_top3, _cap_nind, _cap_size = _pc.concentration(_ind_counts)
    return {"ok": True, "rows": out_rows, "universe_size": len(universe),
            "scanned": scanned, "elapsed": round(_t.time() - _t0, 1),
            "pool_candidates": _pool_before, "pool_capped": _pool_capped,
            "pool_max": _pool_max, "external_prefetch": _external_prefetch,
            # 2026-10-30：行业上限落地后的池子披露（与回测口径一致）
            "industry_cap": {
                "max_per_industry": _cap_per, "max_industry_pct": _cap_pct,
                "applied": bool((_cap_per and _cap_per > 0) or (_cap_pct and _cap_pct > 0)),
                "pool_size": _cap_size, "n_industries": _cap_nind,
                "top1_share": round(_cap_top1, 4), "top3_share": round(_cap_top3, 4),
                "counts": _ind_counts,
            },
            "params": {"limit": limit, "box_window": box_window, "low_pct": low_pct,
                       "quality": quality,
                       "w_shape": w_shape, "w_fund": w_fund, "w_worth": w_worth,
                       "note": "综合分=形态%.0f%%+基本面%.0f%%+长窗worth(250/500/750·30:40:30)%.0f%%（缺项摊派）"
                               "+国资系加分0~15；质量档=%s；PE/ROE/负债率/股息率/实控人已接入"
                               % (w_shape, w_fund, w_worth, quality or "不限")}}
def api_screener_bottom(min_years: float = 0, state: str = "all", pool: str = "market",
                        codes: str = "", limit: int = 100, refresh: int = 0,
                        include_st: int = 0, prefix: str = "", min_score: float = 0,
                        medal: str = "all") -> dict:
    """GET /api/screener_bottom —— 板块三：长期底部埋伏（全市场 · 自适应平台）。

    参数：
      min_years  底部时长下限（0=全部；1/2/3/5/8 对应进哥定的分档）
      state      all | dormant（仅埋伏中·未启动）| started（仅刚启动/突破）
      pool       market（全市场）| watch（只留关注池）| custom（只留 codes 指定）
      codes      逗号分隔代码（pool=custom 时生效）
      refresh    1=绕过缓存强制重扫
      prefix     2026-10-21 进哥需求：代码开头筛选（""=全部 / "0" / "3" / "6"）
      min_score  2026-10-21 进哥需求：埋伏分下限（0=全部；60/70/80…）
      medal      2026-10-21 进哥需求：奖牌等级（all=全部 / "3"金牌 / "2"银牌 / "1"铜牌 / "0"无牌），
                 口径=行业龙头分级（金牌=行业综合排名前5%、银=前10%、铜=前15%，与个股徽章同一套）
    返回 counts 为「≥N 年」的累计只数（在 pool/state 过滤之后统计），供前端分档按钮显示数量；
    medal_counts / prefix_counts 同口径（pool/state/st 之后、其余新筛选之前），供奖牌/前缀 chip 显示数量。
    """
    limit = max(5, min(int(limit or 100), 300))
    try:
        min_years = max(0.0, min(float(min_years or 0), 20.0))
    except (TypeError, ValueError):
        min_years = 0.0
    state = state if state in ("all", "dormant", "started") else "all"
    pool = pool if pool in ("market", "watch", "custom") else "market"
    # ── 2026-10-21 进哥需求：奖牌 / 代码前缀 / 埋伏分 三组新筛选（参数归一化）──
    prefix = str(prefix or "").strip()
    if prefix not in ("", "0", "3", "6"):
        prefix = ""
    medal = str(medal or "all").strip()
    if medal not in ("all", "3", "2", "1", "0"):
        medal = "all"
    try:
        min_score = max(0.0, float(min_score or 0))
    except (TypeError, ValueError):
        min_score = 0.0

    cached = False
    stale = False
    if not refresh:
        dd = _bottom_cache_load()
        if dd is None:
            # 过期缓存不丢弃：先返回旧数据（stale-while-revalidate），后台静默重扫。
            # 否则用户隔天打开面板，会先看到 20~30 秒空白「正在扫描」。
            dd = _bottom_cache_load(allow_stale=True)
            stale = bool(dd)
    else:
        dd = None
    if dd:
        rows = dd["rows"]
        scanned, elapsed, ts = dd.get("scanned"), dd.get("elapsed"), dd.get("ts")
        cached = True
        if stale:
            try:
                _bottom_scan_async(force=False)
            except Exception:  # noqa: BLE001
                logging.warning("bottom 后台重扫启动失败", exc_info=True)
    else:
        st = _bottom_scan_async(force=bool(refresh))
        if st.get("err"):
            return {"ok": False, "reason": "扫描失败：%s" % st["err"], "rows": [], "total": 0}
        return {"ok": False, "computing": True, "rows": [], "total": 0, "counts": {},
                "all_total": 0, "elapsed": round(time.time() - (st.get("started_at") or time.time()), 1),
                "reason": "首次扫描全市场前复权日K（约 20~30 秒），结果缓存 12 小时"}

    base = rows
    # 默认排除 ST/*ST：退市边缘的「长期底部」不是埋伏标的（实测 748 只里 23 只 ST，
    # 且分数集中在榜首——跌 85%+ 趴 8 年，形态符合但基本面是雷）。要看得手动开。
    if not include_st:
        base = [r for r in base if not r.get("st")]
    if pool == "watch":
        try:
            watchlib.ensure_schema(DB.conn)
            ws = {it["symbol"] for it in (watchlib.list_items(DB.conn) or [])}
            base = [r for r in base if r["code"] in ws]
        except Exception:  # noqa: BLE001
            base = []
    elif pool == "custom":
        want = {c.strip() for c in re.split(r"[\s,，、]+", codes or "") if c.strip()}
        base = [r for r in base if r["code"] in want] if want else []
    if state == "dormant":
        base = [r for r in base if not r["started"]]
    elif state == "started":
        base = [r for r in base if r["started"]]

    # ── 2026-10-21 进哥需求：奖牌 / 代码前缀 / 埋伏分 筛选 ──
    # 分档数量（counts / medal_counts / prefix_counts）统一在 pool/state/ST 之后、本组新筛选之前
    # 统计，与「≥N 年」chip 的既有口径一致：数量反映「当前池子里还有什么可切」，不随自身筛选归零。
    counts = {str(t): sum(1 for r in base if r["years"] >= t) for t in _BOTTOM_TIERS}
    prefix_counts = {p: sum(1 for r in base if str(r["code"]).startswith(p)) for p in ("0", "3", "6")}
    dmap = {}
    try:
        if dragon_mod is not None:
            dmap = (dragon_mod.map_payload() or {}).get("m") or {}
    except Exception:  # noqa: BLE001
        dmap = {}
    if dmap:
        _tiers = {str(r["code"]): int(dmap.get(str(r["code"]), 0)) for r in base}
        medal_counts = {"3": sum(1 for v in _tiers.values() if v == 3),
                        "2": sum(1 for v in _tiers.values() if v == 2),
                        "1": sum(1 for v in _tiers.values() if v == 1),
                        "0": sum(1 for v in _tiers.values() if v == 0)}
    else:
        medal_counts = {}
    # 应用三组新筛选（顺序：前缀 → 埋伏分 → 奖牌；奖牌筛选失败要给出明确原因，绝不静默放行）
    if prefix:
        base = [r for r in base if str(r["code"]).startswith(prefix)]
    if min_score:
        base = [r for r in base if r.get("score") is not None and float(r["score"]) >= min_score]
    if medal != "all":
        if not dmap:
            return {"ok": False,
                    "reason": "龙头分级数据尚未生成（奖牌筛选依赖它），约 1~2 分钟后自动重建，请稍候重试",
                    "rows": [], "total": 0, "counts": counts, "prefix_counts": prefix_counts,
                    "medal_counts": {}, "all_total": 0}
        want = int(medal)
        base = [r for r in base if int(dmap.get(str(r["code"]), 0)) == want]
    out = [r for r in base if r["years"] >= min_years] if min_years else list(base)
    total = len(out)
    out = out[:limit]
    return {"ok": True, "rows": out, "total": total, "counts": counts,
            "medal_counts": medal_counts, "prefix_counts": prefix_counts,
            "all_total": len(rows), "scanned": scanned, "elapsed": elapsed,
            "ts": ts, "cached": cached, "stale": stale, "computing": bool(stale),
            "params": {"min_years": min_years, "state": state, "pool": pool,
                       "prefix": prefix, "min_score": min_score, "medal": medal,
                       "limit": limit, "tiers": list(_BOTTOM_TIERS),
                       "note": "自适应底部平台：压缩比≤0.85 + 近一年止跌 + 箱内位置≤0.75；"
                               "埋伏分=时长32%%+低位22%%+回撤18%%+横盘18%%+启动10%%"}}
