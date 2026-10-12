# -*- coding: utf-8 -*-
"""增强功能④ 多资产行情（指数 / ETF / 黄金 / 国债 / 美股一网打尽）。

secid 规则：沪 1. / 深 0. / 美股指数 100. / 美股个股 105. / 港股 116.。

数据链路（2026-09-27 改造）：
  · 第一站：push2.eastmoney.com ulist.np 多 ut + push2his 端点 fallback（emapi.ulist 已升级）
  · 第二站：腾讯 qt.gtimg.cn 逐只 snapshot（emapi.tencent_quote）
  · 失败逐只拼接：每只单独标 ok=True/False，便于前端清楚地看到"哪只取到/哪只没取到"。
"""
from __future__ import annotations
import emapi

# (secid, 展示名, 板块标签)
PRESETS = [
    ("1.000001",  "上证指数",    "A股指数"),
    ("0.399001",  "深证成指",    "A股指数"),
    ("1.000300",  "沪深300",    "A股指数"),
    ("0.399006",  "创业板指",    "A股指数"),
    ("1.000688",  "科创50",      "A股指数"),
    ("1.510300",  "沪深300ETF", "ETF"),
    ("1.518880",  "黄金ETF",    "黄金"),
    ("1.511010",  "国债ETF",    "国债"),
    ("1.513100",  "纳指ETF",    "海外ETF"),
    ("1.513500",  "标普500ETF", "海外ETF"),
    ("0.159915",  "创业板ETF",  "ETF"),
    ("100.NDX",   "纳斯达克100", "美股指数"),
    ("100.SPX",   "标普500",    "美股指数"),
    ("105.AAPL",  "苹果",       "美股个股"),
    ("105.NVDA",  "英伟达",     "美股个股"),
    ("105.TSLA",  "特斯拉",     "美股个股"),
]


def _label_for(sid: str) -> str:
    for s, name, lbl in PRESETS:
        if s == sid:
            return lbl
    return "—"


def _name_for(sid: str) -> str:
    for s, name, lbl in PRESETS:
        if s == sid:
            return name
    return ""


def _em_preset_rows(secids: list) -> dict:
    """东财 ulist（emapi.ulist 内部已多 ut + push2his 降级）→ {secid: {code,name,price,pct,src}}。"""
    rows = emapi.ulist(secids, fields="f2,f3,f12,f14")
    out: dict = {}
    for i, sid in enumerate(secids):
        r = rows[i] if i < len(rows) else None
        if r and (r.get("price") is not None or r.get("name")):
            out[sid] = {"code": r.get("code") or sid.split(".")[-1], "name": r.get("name"),
                        "price": r.get("price"), "pct": r.get("pct"), "src": "push2"}
    return out


def _tencent_preset_rows(secids: list) -> dict:
    """腾讯**单请求**批量（emapi.tencent_quote_multi）→ {secid: {code,name,price,pct,src}}。"""
    q = emapi.tencent_quote_multi(secids, timeout=3)
    out: dict = {}
    for sid, r in (q or {}).items():
        if r and r.get("price") is not None:
            out[sid] = {"code": r.get("code") or sid.split(".")[-1], "name": r.get("name"),
                        "price": r.get("price"), "pct": r.get("pct"), "src": "tencent"}
    return out


def preset() -> dict:
    """预设标的多资产快照：东财 ulist 与腾讯批量**并发**竞速，谁先回用谁（2026-10-09 改造）。

    旧实现是「push2 ulist 先跑（8s×多 ut×双端点）→ 不通再逐只腾讯」的**串行降级**，
    push2 被本机风控时会把面板拖到十几秒。现改为两源**同时发**（emapi.race_merge）：
    先回的先采纳、慢源到点即丢，整体硬上限 2.5s。
    - `_merge` 有值不回退；`_complete` = 全部标的都拿到有效价 → 立即收工；
    - 逐只独立标 src（push2 / tencent），**返回结构不变**（code/name/price/pct/market/src + 顶层 ok/rows/src/n_ok）。
    """
    secids = [s for s, _, _ in PRESETS]

    def _merge(acc: dict, result: dict) -> None:
        for sid, r in (result or {}).items():
            if sid not in acc or (r.get("price") is not None and acc[sid].get("price") is None):
                acc[sid] = r

    def _complete(acc: dict) -> bool:
        return all(sid in acc and acc[sid].get("price") is not None for sid in secids)

    found = emapi.race_merge(
        [lambda: _em_preset_rows(secids), lambda: _tencent_preset_rows(secids)],
        _merge, _complete, timeout=2.5)

    out: list = []
    for sid in secids:
        disp_label = _label_for(sid)
        disp_name = _name_for(sid)
        r = found.get(sid)
        if r and r.get("price") is not None:
            out.append({"code": r.get("code") or sid.split(".")[-1],
                        "name": r.get("name") or disp_name,
                        "price": r.get("price"), "pct": r.get("pct"),
                        "market": disp_label, "src": r.get("src")})
        else:
            out.append({"code": sid.split(".")[-1], "name": disp_name,
                        "price": None, "pct": None,
                        "market": disp_label, "src": "—"})
    n_ok = sum(1 for r in out if r.get("price") is not None)
    src = "push2" if any((found.get(s) or {}).get("src") == "push2" for s in found) else "tencent"
    if n_ok == 0:
        return {"ok": False, "rows": out, "src": src,
                "reason": "未取到行情（东财与腾讯都不可达，检查网络/代理/VPN）"}
    return {"ok": True, "rows": out, "src": src, "n_ok": n_ok}


def lookup(symbol: str) -> dict:
    """单只查询：东财 push2 与腾讯**并发**竞速，谁先回用谁（整体硬上限 3s）；都失败报失败。
    symbol 可输入：6 位代码 / 'usAAPL' / '510300' / '1.510300'。"""
    s = (symbol or "").strip()
    if not s:
        return {"ok": False, "symbol": symbol, "reason": "请输入代码"}
    sid = emapi.secid(s)

    def _em():
        q = emapi.quote(sid, timeout=3, retries=0)
        if q and q.get("price") is not None:
            return {"ok": True, "symbol": s, "code": q.get("code") or sid.split(".")[-1],
                    "name": q.get("name") or "", "price": q.get("price"),
                    "pct": q.get("pct"), "src": "push2"}
        return None

    def _tx():
        tq = emapi.tencent_quote(s, timeout=3)
        if tq and tq.get("price") is not None:
            return {"ok": True, "symbol": s, "code": tq["code"],
                    "name": tq.get("name") or "", "price": tq.get("price"),
                    "pct": tq.get("pct"), "src": "tencent"}
        return None

    r = emapi.race_first([_em, _tx], timeout=3.0)
    if r:
        return r
    return {"ok": False, "symbol": s, "reason": "未取到行情（代码有误或全部接口不可达）"}
