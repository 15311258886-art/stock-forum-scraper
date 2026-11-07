# -*- coding: utf-8 -*-
"""board 域 API 处理函数（从 viewer/server.py 物理抽取，2026-11-07 拆分重构）。

抽取规则：函数体逻辑零改动；依赖的 server 模块级符号经 `from viewer.server import (...)`
重定向进本地命名空间；标准库本地导入。server.py 末尾 re-export 保持命名空间兼容，
routes.py 与既有测试（S.api_*）无需改动。
"""
from __future__ import annotations

import logging, time
from viewer.server import (  # noqa: E402,F401
    DB,
    _BOARD_TTL,
    _em_secid,
    _flow_range_cached,
    _num,
    _sina_stock_moneyflow,
    _stock_moneyflow_any,
    board_flow,
    board_flow_range,
    cf,
    valuation,
    _BOARD_CACHE,
)

def api_board_flow_snapshot(kind: str = "industry", pz: int = 60, force: int = 0) -> dict:
    """板块资金流排行快照（带数据来源标签）。"""
    board_flow.ensure_schema(DB.conn)
    kind = kind if kind in board_flow.KINDS else board_flow.DEFAULT_KIND
    return board_flow.snapshot(kind=kind, pz=pz, force=bool(force))
def api_board_flow_intraday(kind: str = "industry", top: int = 12, force: int = 0) -> dict:
    """板块资金流分时曲线（东财实时 / 采样累积 / 库内快照 三级兜底）。"""
    board_flow.ensure_schema(DB.conn)
    kind = kind if kind in board_flow.KINDS else board_flow.DEFAULT_KIND
    return board_flow.intraday(kind=kind, top=top, force=bool(force), conn=DB.conn)
def api_board_flow_range(start: str = "", end: str = "", kind: str = "industry",
                         top: int = 16, force: int = 0) -> dict:
    """板块资金流「日期区间」聚合（2026-10-05，王总：累计流入要能指定起止日期）。

    与「单日分时曲线」是两套数据形态：这里是**日线历史**（每个交易日一个点），
    那里是**当日分钟累计**。两个 mode 在前端并存，互不串味。
    """
    try:
        board_flow_range.ensure_schema(DB.conn)
        res = board_flow_range.range_flow(
            DB.conn, start=start, end=end, kind=kind, top=top, force=bool(force))
        res["force"] = bool(force)
        return _flow_range_cached("range:%s:%s:%s:%d:%d" % (start, end, kind, top, force), res)
    except Exception as exc:  # noqa: BLE001
        # 2026-10-31 修复：原为 `logger.exception(...)`，本函数的局部名表里没有 `logger`
        # → 异常处理自身抛 NameError。改用模块内既有写法 logging.getLogger(__name__)。
        logging.getLogger(__name__).exception("板块资金流区间聚合异常")
        return {"ok": False, "error": "区间聚合失败：%s" % exc, "series": [], "pool": []}
def api_board_flow_dates(kind: str = "industry") -> dict:
    """区间日期框的可选范围（交易日历 ∪ 库内已有日线日期）。"""
    try:
        board_flow_range.ensure_schema(DB.conn)
        return board_flow_range.date_options(DB.conn, kind)
    except Exception as exc:  # noqa: BLE001
        # 2026-10-31 修复：同 api_board_flow_range，`logger` 未定义 → 改模块内既有写法。
        logging.getLogger(__name__).exception("板块资金流日期清单异常")
        return {"ok": False, "error": str(exc), "days": []}
def api_board_flow_status() -> dict:
    """板块资金流自检：两源连通性 + 采样存量 + 当前曲线来源。"""
    board_flow.ensure_schema(DB.conn)
    return board_flow.status(conn=DB.conn)
def api_board(code: str, kind: str = "industry", name: str = "", force: int = 0) -> dict:
    """板块详情页聚合：成分股全列表 + 板块行情/资金 + 板块内个股资金对比 + 板块内龙虎榜 + 估值分位。

    数据全部复用现成稳定源（不新接脆弱接口、不触东财风控）：
      · 成分股：board_flow.board_members（新浪 getHQNodeData，单板块上限 100 只）
      · 板块行情/资金：board_flow.snapshot 按 board_code 取该行
      · 板块内个股资金对比：_stock_moneyflow_any 批量（东财主源 → 新浪兜底，前 20 只）
      · 板块内龙虎榜：本地 lhb_stocks（零网络）
      · 估值分位：valuation.score 批量（前 30 只，已有缓存）
    """
    if not code:
        return {"ok": False, "reason": "缺少板块代码（board_code）"}
    kind = kind if kind in board_flow.KINDS else board_flow.DEFAULT_KIND
    ck = (code, kind)
    if not force:
        _bh = _BOARD_CACHE.get(ck)
        if _bh and time.time() - _bh[0] < _BOARD_TTL:
            return _bh[1]
    # ① 成分股全列表
    members = board_flow.board_members(code, 100) or []
    if not members:
        return {"ok": False, "reason": "未取到该板块成分股（代码可能失效或新浪源不可达）", "code": code}
    codes = [m["code"] for m in members]
    # ② 板块行情/资金
    snap = board_flow.snapshot(kind=kind, pz=200, force=bool(force))
    brow = next((r for r in snap.get("rows", []) if r.get("board_code") == code), None)
    # ③ 板块内个股资金对比（前 20 只并发；新浪主源，东财仅作兜底——与本机 board_flow 架构一致，
    #    避免逐只打东财 fflow（本机东财常不可达，每只 ~4s 才失败）拖出 14s 冷启动）
    flow_top = members[:20]
    flow_map: dict = {}

    def _board_flow_one(m):
        sym = m["code"]
        try:
            mk, cd, _ = _em_secid(sym)
        except Exception:  # noqa: BLE001
            mk, cd = ("1" if str(sym).startswith(("6", "9")) else "0"), str(sym)
        s = _sina_stock_moneyflow(cd, mk)
        if isinstance(s, dict) and s.get("ok"):
            return s
        return _stock_moneyflow_any(sym)  # 东财兜底（含四档明细）

    try:
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_board_flow_one, m): m["code"] for m in flow_top}
            for fut in futs:
                c = futs[fut]
                try:
                    flow_map[c] = fut.result(timeout=12) or {}
                except Exception:  # noqa: BLE001
                    flow_map[c] = {}
    except Exception:  # noqa: BLE001
        pass
    # ④ 板块内龙虎榜（最近交易日，本地库）
    _lhb_row = DB.conn.execute("SELECT MAX(trade_date) AS d FROM lhb_stocks").fetchone()
    lhb_date = _lhb_row["d"] if _lhb_row else None
    lhb_rows = []
    if lhb_date and codes:
        ph = ",".join("?" * len(codes))
        for r in DB.conn.execute(
                f"SELECT security_code, security_name, change_rate, net_amt, reason "
                f"FROM lhb_stocks WHERE trade_date=? AND security_code IN ({ph})",
                (lhb_date, *codes)):
            lhb_rows.append({"code": r["security_code"], "name": r["security_name"],
                             "pct": _num(r["change_rate"]),
                             "net_yi": round((_num(r["net_amt"]) or 0.0) / 1e8, 2),
                             "reason": r["reason"]})
        lhb_rows.sort(key=lambda x: -(x["net_yi"] or 0))
    # ⑤ 估值分位（前 30 只并发）
    val_map: dict = {}
    try:
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(valuation.score, m["code"]): m["code"] for m in members[:30]}
            for fut in futs:
                c = futs[fut]
                try:
                    v = fut.result(timeout=8) or {}
                    val_map[c] = {"score": v.get("score"), "pe": v.get("pe"), "roe": v.get("roe")}
                except Exception:  # noqa: BLE001
                    val_map[c] = {}
    except Exception:  # noqa: BLE001
        pass
    for m in members:
        c = m["code"]
        mf = flow_map.get(c) or {}
        m["main_net"] = mf.get("main_net")
        m["main_pct"] = mf.get("main_pct")
        m["val_score"] = (val_map.get(c) or {}).get("score")
    scores = [x for x in (m.get("val_score") for m in members[:30]) if isinstance(x, (int, float))]
    val_stat = {"count": len(scores),
                "avg": round(sum(scores) / len(scores), 1) if scores else None,
                "n_overweight": sum(1 for s in scores if s >= 60),
                "n_neutral": sum(1 for s in scores if 40 <= s < 60),
                "n_low": sum(1 for s in scores if s < 60)} if scores else {"count": 0}
    flow_sorted = sorted(
        [{"code": m["code"], "name": m["name"], "main_net": m.get("main_net"), "pct": m.get("pct")}
         for m in members if m.get("main_net") is not None],
        key=lambda x: -(x["main_net"] or 0))
    payload = {
        "ok": True, "code": code, "kind": kind,
        "board_name": name or (brow or {}).get("board_name") or code,
        "board": brow,
        "member_total": len(members),
        "members": members,
        "flow_compare": flow_sorted[:30],
        "lhb": {"date": lhb_date, "rows": lhb_rows},
        "valuation": val_stat,
        "note": "成分股取自新浪（单板块上限 100 只）；资金/估值为板块内前 N 只聚合，非全量精算；"
                "仅供研究参考，非投资建议。",
    }
    _BOARD_CACHE[ck] = (time.time(), payload)
    return payload
