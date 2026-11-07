# -*- coding: utf-8 -*-
"""watch 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import datetime, logging, time
from viewer.server import (  # noqa: E402,F401
    DB,
    _WRITE_LOCK,
    _api_cache_get,
    _api_cache_get_any,
    _api_cache_put,
    _mainline_map,
    _num,
    _swr_refresh,
    _watch_prices,
    bili_scan,
    postdigest,
    screener,
    watchlib,
    avoid_mod,
    lift_mod,
)

def api_watch_brief() -> dict:
    """「我的票」置顶「今日提示」（P0-F）：关注池今天有没有事，而不是再给一堆工具。
    五类提示：① 到价触发（未读优先）② 接近目标价 ③ 池内当日异动（现价/涨跌）④ 池内被看空·分歧
    ⑤ 池内落在今日主线的票。并显式报出「开了提醒却没设目标价」的票 —— 那些提醒永远不会响。"""
    log = logging.getLogger(__name__)
    out: dict = {"ok": True, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "total": 0, "unread": 0, "groups": [], "alerts": [], "quotes": [],
                 "movers": [], "near": [], "no_target": [], "caution": [],
                 "on_mainline": [], "mainline_date": None, "quote_src": "", "basis": ""}
    mdate, mmap = _mainline_map()
    out["mainline_date"] = mdate

    items = []
    prices: dict = {}
    try:
        watchlib.ensure_schema(DB.conn)
        items = watchlib.list_items(DB.conn) or []
        out["total"] = len(items)
        out["unread"] = watchlib.count_unread(DB.conn)
        gmap: dict = {}
        for it in items:
            g = it.get("group_name") or "未分组"
            gmap[g] = gmap.get(g, 0) + 1
        out["groups"] = [{"name": k, "n": v} for k, v in sorted(gmap.items(), key=lambda x: -x[1])]
        for a in (watchlib.list_alerts(DB.conn, limit=50) or [])[:10]:
            out["alerts"].append({"symbol": a.get("symbol"), "name": a.get("name") or "",
                                  "rule": a.get("rule"), "target_price": _num(a.get("target_price")),
                                  "price": _num(a.get("triggered_price")),
                                  "triggered_at": a.get("triggered_at"), "read": a.get("read")})

        # 现价/当日涨跌：本地缓存的行情只覆盖极少数池内票（实测 3/14），所以走三源并发批量报价
        # （_watch_prices，整体硬上限 3s，失败即 None —— 前端显 «—»，绝不写 0）。
        codes = [str(it.get("symbol") or "") for it in items if it.get("symbol")]
        if codes:
            prices = _watch_prices(codes, budget=3.0) or {}
        out["quote_src"] = "多源并发" if prices else "未取到"
        for it in items:
            q = prices.get(str(it.get("symbol") or "")) or {}
            price, pct = _num(q.get("price")), _num(q.get("pct"))
            if price is None:
                continue
            out["quotes"].append({"symbol": it.get("symbol"), "name": it.get("name") or "",
                                  "price": price, "pct": pct})
        out["movers"] = sorted([q for q in out["quotes"] if q["pct"] is not None],
                               key=lambda x: -abs(x["pct"]))[:8]

        near = []
        for it in items:
            tp = _num(it.get("target_price"))
            if tp is None or tp <= 0:
                # 开了提醒却没有目标价 → 这条规则永远不可能触发。明确报出来，别让它静默失效。
                if it.get("alert_on"):
                    out["no_target"].append({"symbol": it.get("symbol"),
                                             "name": it.get("name") or ""})
                continue
            q = prices.get(str(it.get("symbol") or "")) or {}
            lp = _num(q.get("price"))
            if lp is None:
                lp = _num(it.get("last_price"))
            if lp is None:
                continue
            dist = round((lp - tp) / tp * 100.0, 2)
            near.append({"symbol": it.get("symbol"), "name": it.get("name") or "",
                         "target_price": tp, "last_price": lp, "dist_pct": dist,
                         "dir": it.get("alert_dir") or "", "group": it.get("group_name") or ""})
        near.sort(key=lambda x: abs(x["dist_pct"]))
        out["near"] = near[:8]
        # ④ 池内落在今日主线的票（+ 是否仍在「可推进」名单）
        for it in items:
            c = str(it.get("symbol") or "")
            ml = mmap.get(c)
            if not ml:
                continue
            out["on_mainline"].append({
                "symbol": c, "name": it.get("name") or "",
                "sector": ml.get("sector") or "", "pct": ml.get("pct"),
                "forward": bool(ml.get("forward")),
            })
        out["near"] = near[:8]
    except Exception as e:  # noqa: BLE001
        log.warning("watch_brief 关注池失败: %s", e)

    # ② 池内被看空/分歧的票（只认真分歧，挡掉"中立（没明确观点）"这类无方向样本）
    try:
        pd = postdigest.build(hours=72)
        vmap = {s.get("key"): s for s in (pd.get("stocks") or []) if s.get("key")}
        for it in items:
            s = vmap.get(str(it.get("symbol") or ""))
            if not s:
                continue
            pos, neg = s.get("pos") or 0, s.get("neg") or 0
            vk = s.get("verdict_key")
            if vk == "bear" or (vk == "diverge" and pos > 0 and neg > 0):
                out["caution"].append({
                    "symbol": it.get("symbol"), "name": it.get("name") or "",
                    "tone": vk, "text": s.get("verdict") or "",
                    "pos": pos, "neg": neg,
                })
    except Exception as e:  # noqa: BLE001
        log.warning("watch_brief 情绪交叉失败: %s", e)

    out["basis"] = ("口径：现价/涨跌=新浪·腾讯·东财三源并发批量报价（本机行情源不可达时显 «—»，不写 0）；"
                    "到价提醒来自关注池规则触发（未读优先）；接近目标价=|现价−目标价|/目标价；"
                    "看空/分歧取自近 72 小时帖子情绪（与「汇总·情绪速览」同一把尺子，只认真分歧）；"
                    "主线归属取 sector_chain 今日数据日 %s。提醒是提示，不是买卖建议。" % (mdate or "—"))
    return out
def api_watch_list(force=False) -> dict:
    """自选池全量：分组 + 标的（附最新价 / 涨跌 / 关注天数 / 距目标价 / 加入至今涨幅）+ 未读数。

    2026-10-25：取价要竞速三个行情源，实测 14s。同样改为 SWR —— 过期先给上次快照 + 后台重取价。
    """
    if not force:
        cached = _api_cache_get("watch_list")
        if cached is not None:
            return cached
        snapshot, age = _api_cache_get_any("watch_list")
        if isinstance(snapshot, dict):
            _swr_refresh("watch_list", lambda: api_watch_list(force=True))
            snap = dict(snapshot)
            snap["_stale"] = True
            snap["_cache_age"] = int(age or 0)
            return snap
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        watchlib.seed_default_groups(DB.conn)
        groups = watchlib.list_groups(DB.conn)
        items = watchlib.list_items(DB.conn)
        unread = watchlib.count_unread(DB.conn)
    prices: dict = {}
    if items:
        try:
            prices = _watch_prices([it["symbol"] for it in items])
        except Exception:  # noqa: BLE001 —— 取价失败不拖垮列表，价格留空（前端显 «—»）
            logging.warning("自选池取价失败", exc_info=True)
    for it in items:
        p = prices.get(it["symbol"]) or {}
        price, tgt, pap = p.get("price"), it.get("target_price"), it.get("price_at_add")
        it["price"] = price
        it["pct"] = p.get("pct")
        if not it.get("name"):
            it["name"] = p.get("name") or ""
        it["to_target_pct"] = round((tgt / price - 1) * 100, 2) if (price and tgt) else None
        it["since_add_pct"] = round((price / pap - 1) * 100, 2) if (price and pap) else None
    # —— 关注池列整改（2026-10-12 进哥需求）：注入「入手值得率 / 博主提及 / 战法信号」三组数据 ——
    # 纪律：三个数据源各自独立 try/except，任一失败只吞异常、缺失即 None/[]，绝不拖垮整个列表。
    symbols = [it["symbol"] for it in items]

    # 1) 入手值得率：复用 screener 评分机器，返回 {code: 0~100 或 None}。按整批取（函数内部按交易日缓存）。
    worth_map: dict = {}
    try:
        worth_map = screener._worth_scores(symbols) or {}
    except Exception:  # noqa: BLE001 —— 评分失败不阻塞列表
        logging.warning("关注池入手值得率评分失败", exc_info=True)

    # 2) 避坑标记：替代原「博主提及」（2026-10-30 博主维度下线）。按整批实时算，
    #    数据源是当日 K 线 + 近 15 日利空公告，**不读任何沉淀快照** —— 陈旧无从谈起。
    avoid_map: dict = {}
    try:
        if avoid_mod is not None:
            avoid_map = avoid_mod.compute(DB.conn, symbols)
    except Exception:  # noqa: BLE001 —— 标记失败不阻塞列表（缺失即 None）
        logging.warning("关注池避坑标记计算失败", exc_info=True)

    # 2b) 限售解禁：未来 60 个交易日内有值得说的事件才返回（见 viewer/lift.py 分档依据）。
    #     与 avoid 语义不同：avoid 是「已经跌了」，lift 是「将要解禁」，故并列下发不合并成分。
    lift_map: dict = {}
    try:
        if lift_mod is not None:
            lift_map = lift_mod.compute(DB.conn, symbols)
    except Exception:  # noqa: BLE001
        logging.warning("关注池解禁标记计算失败", exc_info=True)

    # 3) 战法信号：为池内票单独复算（2026-10-14 修复匹配缺陷——此前只匹配 result 缓存
    #    Top200 截断榜，关注池只命中 1~5 条的票全被挡在榜外，界面战法列全显 «—»）。
    #    口径与全市场扫描同一份 build_signals，基于最新一根日K；signal_date 随接口
    #    下发到前端标注数据日期（盘中最后一根未收盘，信号以收盘复核为准）。
    strat_map: dict = {}
    signal_date = None
    try:
        sig = bili_scan.signals_for_watch(symbols)
        strat_map = sig.get("by_code") or {}
        signal_date = sig.get("signal_date")
    except Exception:  # noqa: BLE001 —— 战法复算失败不阻塞列表（缺失即 «—»）
        logging.warning("关注池战法信号复算失败", exc_info=True)

    for it in items:
        sym = it["symbol"]
        it["worth"] = worth_map.get(sym)
        it["avoid"] = avoid_map.get(sym)
        it["lift"] = lift_map.get(sym)
        it["strat_hits"] = strat_map.get(sym, [])
    result = {"ok": True, "groups": groups, "items": items, "alerts_unread": unread,
              "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "signal_date": signal_date}
    _api_cache_put("watch_list", result, 300)
    return result
def api_watch_item_add(payload: dict) -> dict:
    """加入自选池。名称 / 加入价缺失时用一次轻量取价补全（不阻断，失败留空）。"""
    sym = watchlib._norm_symbol(payload.get("symbol"))
    if not sym:
        return {"ok": False, "error": "缺少股票 / 基金代码"}
    prices: dict = {}
    try:
        prices = _watch_prices([sym])
    except Exception:  # noqa: BLE001
        pass
    q = prices.get(sym) or {}
    name = (payload.get("name") or "").strip() or q.get("name") or ""
    price_at_add = payload.get("price_at_add")
    if price_at_add is None:
        price_at_add = q.get("price")
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        watchlib.seed_default_groups(DB.conn)
        res = watchlib.add_item(
            DB.conn, sym, name=name, kind=payload.get("kind") or "stock",
            group_id=payload.get("group_id"),
            groups=payload.get("groups") if isinstance(payload.get("groups"), list) else None,
            sector=payload.get("sector") or "",
            note=payload.get("note") or "", attention=payload.get("attention", 3),
            target_price=payload.get("target_price"), alert_dir=payload.get("alert_dir") or "",
            alert_on=payload.get("alert_on", 1), price_at_add=price_at_add)
        if res.get("ok"):
            watchlib.check_alerts(DB.conn, prices)     # 加入即刻判一次
    return res
def api_watch_item_update(payload: dict) -> dict:
    iid = payload.get("id")
    if not iid:
        return {"ok": False, "error": "缺少 id"}
    fields = {k: payload[k] for k in ("name", "kind", "group_id", "sector", "note",
                                      "attention", "target_price", "alert_dir", "alert_on")
              if k in payload}
    groups = payload.get("groups")
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        res = watchlib.update_item(DB.conn, int(iid), **fields)
        # 多对多分组（2026-10-14）：groups = 全量设置（编辑弹窗多选）；空数组 → 回退默认组
        if res.get("ok") and isinstance(groups, list):
            gres = watchlib.set_groups(DB.conn, int(iid), groups)
            if not gres.get("ok"):
                return gres
        return res
def api_watch_item_reorder(payload: dict) -> dict:
    """组内排序：{group_id, ids:[...]} → 按给定顺序重写该组 sort_order（10,20,30…）。"""
    gid = payload.get("group_id")
    ids = payload.get("ids")
    if gid is None or not isinstance(ids, list):
        return {"ok": False, "error": "缺少 group_id / ids"}
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.reorder_in_group(DB.conn, int(gid), ids)
def api_watch_item_group_remove(payload: dict) -> dict:
    """移出单个分组：{id, group_id} → 标的仍在池中（其他组保留；无剩余组回退默认组）。"""
    iid = payload.get("id")
    gid = payload.get("group_id")
    if not iid or gid is None:
        return {"ok": False, "error": "缺少 id / group_id"}
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.remove_from_group(DB.conn, int(iid), int(gid))
def api_watch_item_remove(payload: dict) -> dict:
    iid = payload.get("id")
    if not iid:
        return {"ok": False, "error": "缺少 id"}
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.remove_item(DB.conn, int(iid))
def api_watch_group_add(payload: dict) -> dict:
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.add_group(DB.conn, payload.get("name") or "",
                                  kind=payload.get("kind") or "mix", color=payload.get("color") or "")
def api_watch_group_rename(payload: dict) -> dict:
    gid = payload.get("id")
    if not gid:
        return {"ok": False, "error": "缺少 id"}
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.rename_group(DB.conn, int(gid), payload.get("name") or "")
def api_watch_group_remove(payload: dict) -> dict:
    gid = payload.get("id")
    if not gid:
        return {"ok": False, "error": "缺少 id"}
    rt = payload.get("reassign_to")
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.remove_group(DB.conn, int(gid),
                                     reassign_to=(int(rt) if str(rt).lstrip("-").isdigit() else None))
def api_watch_group_reorder(payload: dict) -> dict:
    """分组排序（2026-10-14 进哥：分组管理弹窗 ↑/↓ 调序）。ids = 完整分组 id 列表（新顺序）。"""
    ids = payload.get("ids")
    if not isinstance(ids, list) or not ids:
        return {"ok": False, "error": "缺少 ids"}
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return watchlib.reorder_groups(DB.conn, ids)
def api_watch_check() -> dict:
    """到价检测：取最新价 → 命中规则则生成未读告警（同标的同方向同目标价不重复）。"""
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        codes = sorted({it["symbol"] for it in watchlib.list_items(DB.conn)})
    prices: dict = {}
    if codes:
        try:
            prices = _watch_prices(codes)
        except Exception:  # noqa: BLE001
            logging.warning("自选池到价检测取价失败", exc_info=True)
    with _WRITE_LOCK:
        res = watchlib.check_alerts(DB.conn, prices)
        res["unread"] = watchlib.count_unread(DB.conn)
    res["ok"] = True
    return res
def api_watch_alerts(limit: int = 200, only_unread: bool = False) -> dict:
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        return {"ok": True, "alerts": watchlib.list_alerts(DB.conn, limit=limit, only_unread=only_unread),
                "unread": watchlib.count_unread(DB.conn)}
def api_watch_alert_read(payload: dict) -> dict:
    with _WRITE_LOCK:
        watchlib.ensure_schema(DB.conn)
        if payload.get("all"):
            return watchlib.mark_all_read(DB.conn)
        aid = payload.get("id")
        if not aid:
            return {"ok": False, "error": "缺少 id"}
        return watchlib.mark_alert(DB.conn, int(aid), bool(payload.get("read", True)))
