# -*- coding: utf-8 -*-
"""funnel 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging
from viewer.server import (  # noqa: E402,F401
    DB,
    _resonance_dir_global,
    api_douyin_plate,
    bili_scan,
    avoid_mod,
    lift_mod,
)

def api_funnel_score(plate, mode="sentiment"):
    """方法共振漏斗：给定板块+模式，产出候选池 + 每只票的四方法归一化方向。

    mode=sentiment(默认): 池来自 api_douyin_plate（豆包舆情文本挖名）
    mode=official       : 池来自 plate_members（东财概念/行业板块全量成员，
                          由 tools/fetch_plate_members.py 抓取落库）
    四方法方向统一为 dir in {看涨,看跌,中性,未覆盖}。
    """
    out = {"ok": True, "plate": plate, "mode": mode, "pool": [], "n": 0}
    if not plate:
        out["note"] = "缺少 plate"; return out
    # 1) 候选池
    pool = []
    if mode == "official":
        try:
            DB.conn.execute(
                "CREATE TABLE IF NOT EXISTS plate_members "
                "(code TEXT, name TEXT, board TEXT, board_code TEXT, updated_at TEXT)")
        except Exception:
            pass
        for code, name in DB.conn.execute(
                "SELECT code,name FROM plate_members WHERE board=?", (plate,)).fetchall():
            pool.append({"code": code, "name": name or code})
    _fell_back = False
    if not pool:
        d = api_douyin_plate(plate)
        for s in d.get("stocks", []):
            pool.append({"code": s["code"], "name": s["name"],
                         "_rdir": s.get("dir"), "_rscore": s.get("score", 50)})
        if mode == "official":
            _fell_back = True   # 官方池未落库，已回退舆情池
    if not pool:
        out["note"] = "该板块暂无候选股"; return out
    if mode == "official" and _fell_back:
        out["official_fallback"] = True
        # P3（2026-10-26）：诚实标注——官方板块成员未采集(plate_members=0)时，回退的是
        # 「博主舆情挖名池」（非官方、且本质是从博主文本挖名，等于在垃圾堆里挑），而非干净宇宙。
        # 已剔除 ST/博主噪声，但候选源仍是博主衍生，必须明确警示，不能假装官方。
        out["official_fallback_note"] = (
            "⚠ 官方板块成员未采集(plate_members=0)，已回退【博主舆情挖名池】"
            "（非官方·候选源即博主衍生，已剔除博主噪声）。请本机跑 "
            "fetch_plate_members.py 补全东财官方全量成员后再用 official 模式。")
    pool_codes = [p["code"] for p in pool]
    # ===== 2026-10-26 干净宇宙：剔除博主噪声/ST/无K线（进哥：不在垃圾堆里挑垃圾）=====
    try:
        from universe_filter import (get_exclusion_pool, clean_universe_size, REASON_LABELS)
    except Exception:
        def get_exclusion_pool():
            return set(), [], {}
        def clean_universe_size():
            return 0, 0, 0
        REASON_LABELS = {}
    _excl_set, _excl_rows, _excl_counts = get_exclusion_pool()
    _removed = {}
    _kept = []
    _reason_of = {r[0]: r[1] for r in _excl_rows}
    for p in pool:
        c = p["code"]
        if c in _excl_set:
            _removed[_reason_of.get(c, "other")] = _removed.get(_reason_of.get(c, "other"), 0) + 1
        else:
            _kept.append(p)
    pool = _kept
    pool_codes = [p["code"] for p in pool]
    out["excluded"] = {"by_reason": _removed, "total": sum(_removed.values()),
                       "labels": REASON_LABELS}
    _cu = clean_universe_size()
    out["clean_universe"] = {"clean": _cu[0], "total_a": _cu[1], "excluded": _cu[2]}
    if not pool:
        out["note"] = "该板块候选股全部被剔除池过滤（ST/博主噪声/无K线），无可入选标的"
        return out
    # 2) 名称字典
    name2code = {}
    for r in DB.conn.execute(
            "SELECT code,name FROM res_stock WHERE name IS NOT NULL AND length(name)>=3"):
        name2code.setdefault(r[1], r[0])
    names_sorted = sorted(name2code.keys(), key=len, reverse=True)
    # 3) 豆包技术方法：全局按名聚合立场
    dt = {c: {"bull": 0, "bear": 0} for c in pool_codes}
    for ti, sm, st in DB.conn.execute(
            "SELECT title,summary,stance FROM douyin_corpus_video WHERE stance IS NOT NULL"):
        txt = (ti or "") + " " + (sm or "")
        found = set()
        for nm in names_sorted:
            if nm in txt:
                found.add(nm)
        found = set(n for n in found if not any(n != m and n in m for m in found))
        for nm in found:
            cd = name2code.get(nm)
            if cd in dt:
                if st in ("看多", "板块机会"):
                    dt[cd]["bull"] += 1
                elif st in ("看空", "风险提示"):
                    dt[cd]["bear"] += 1
    # 4) 共振成分
    card = {r[0]: (r[1], r[2]) for r in DB.conn.execute(
        "SELECT code,tier,res_score FROM res_stock_card")}
    # 2026-10-30：原「博主反向警示」overlay 整体替换为 avoid 避坑标记（见 viewer/avoid.py）。
    # 旧实现的两个硬伤：① n_bloggers 取自 blogger_overlap 这个**全期沉淀快照**
    # （最后更新 2026-10-27，博主源已停），6 月推荐过的票至今仍在绑定；
    # ② 该字段前端从未渲染 —— 算了 4 个月没人读，等于不存在的功能。
    # 新实现不做时效补丁，而是**根本不读博主数据**：只用当日成交量价 + 近 15 日利空公告。
    sig = {}
    for r in DB.conn.execute(
            "SELECT code,direction,COUNT(*) FROM sig_unified GROUP BY code,direction"):
        sig.setdefault(r[0], {})[r[1]] = sig.get(r[0], {}).get(r[1], 0) + r[2]
    # 4b) 避坑标记：整池一次算（数据源 = 当日 K 线 + 近 15 日利空公告，无博主数据）
    avoid_map = {}
    try:
        if avoid_mod is not None:
            avoid_map = avoid_mod.compute(DB.conn, pool_codes)
    except Exception:  # noqa: BLE001 —— 标记失败不阻塞漏斗（缺失即 None）
        logging.warning("候选池避坑标记计算失败", exc_info=True)
    # 4c) 限售解禁（进哥 2026-10-30）：未来 60 个交易日内的解禁批次，整池一次算
    lift_map = {}
    try:
        if lift_mod is not None:
            lift_map = lift_mod.compute(DB.conn, pool_codes)
    except Exception:  # noqa: BLE001
        logging.warning("候选池解禁标记计算失败", exc_info=True)
    # 5) kronos 最新
    kr = DB.conn.execute("SELECT MAX(run_date) FROM kronos_run").fetchone()
    kronos_map = {}
    if kr and kr[0]:
        for r in DB.conn.execute(
                "SELECT code,pred_dir,pred_return FROM kronos_item WHERE run_date=?", (kr[0],)):
            kronos_map[r[0]] = (r[1], r[2])
    # 6) bili 买点票集合
    bili_hits = set()
    try:
        sp = bili_scan.scan_payload(DB.conn, force=False)
        if isinstance(sp, dict) and sp.get("status") == "done":
            for h in sp.get("hits", []):
                if h.get("code"):
                    bili_hits.add(h["code"])
    except Exception:
        pass
    # 7) 组装
    res = []
    for p in pool:
        cd = p["code"]
        c = dt.get(cd, {"bull": 0, "bear": 0})
        if c["bull"] > c["bear"] and c["bull"] > 0:
            dt_dir = "看涨"
        elif c["bear"] > c["bull"] and c["bear"] > 0:
            dt_dir = "看跌"
        elif c["bull"] == 0 and c["bear"] == 0:
            dt_dir = "未覆盖"
        else:
            dt_dir = "中性"
        if mode == "official":
            rd_dir = _resonance_dir_global(cd, card, sig)
        else:
            rd_dir = p.get("_rdir") or "中性"
        if cd in kronos_map:
            kd, kret = kronos_map[cd]
            kronos_dir = "看涨" if kd == "up" else "看跌"
        else:
            kronos_dir = "未覆盖"; kret = None
        bili_dir = "看涨" if cd in bili_hits else ("中性" if bili_hits else "未覆盖")
        av = avoid_map.get(cd)
        res.append({
            "code": cd, "name": p["name"],
            "methods": {
                "kronos": {"dir": kronos_dir, "ret": kret},
                "doubao_tech": {"dir": dt_dir, "bull": c["bull"], "bear": c["bear"],
                                "note": "豆包情绪(不计入正向共识)"},
                "resonance": {"dir": rd_dir,
                              "note": "多源共振(合成卡+六源信号)"},
                "bili_value": {"dir": bili_dir},
            },
            "avoid": av,
            "lift": lift_map.get(cd),
        })
    out["pool"] = res
    out["n"] = len(res)
    return out
