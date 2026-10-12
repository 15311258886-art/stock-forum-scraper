# -*- coding: utf-8 -*-
"""增强功能④ 资讯 / 公告（东财 np-listapi 快讯 + np-anotice-stock 公告）。

实测：7×24 快讯走 getFastNewsList，返回 fastNewsList[].{title,summary,showTime,code}；
个股公告走 np-anotice-stock/api/security/ann，返回 data.list[].{title,notice_date,art_code}。
"""
from __future__ import annotations
import re
import emapi


def market(num: int = 30) -> dict:
    r = emapi.fast_news(num=max(1, min(num, 100)))
    if not r["ok"]:
        return {"ok": False, "rows": [], "reason": r.get("err") or "资讯接口不可达"}
    rows = []
    for it in r["rows"]:
        code = it.get("code") or ""
        rows.append({
            "time": it.get("showTime") or "",
            "title": it.get("title") or it.get("summary") or "",
            "url": ("https://finance.eastmoney.com/a/%s.html" % code) if code else "",
            "type": "快讯",
        })
    return {"ok": True, "rows": rows, "reason": None}


def stock(symbol: str, num: int = 30) -> dict:
    code = re.sub(r"\D", "", symbol or "")
    if not code:
        return {"ok": False, "rows": [], "reason": "请输入 A 股数字代码"}
    r = emapi.announcement(code, num=max(1, min(num, 100)))
    if not r["ok"]:
        return {"ok": False, "rows": [], "reason": r.get("err") or "公告接口不可达"}
    rows = []
    for it in r["rows"]:
        art = it.get("art_code") or ""
        rows.append({
            "time": str(it.get("notice_date") or "")[:10],
            "title": it.get("title") or "",
            "url": ("https://data.eastmoney.com/notices/detail/%s/%s.html" % (code, art)) if art else "",
            "type": "公告",
        })
    return {"ok": True, "rows": rows, "reason": None}
