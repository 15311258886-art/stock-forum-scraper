# -*- coding: utf-8 -*-
"""增强功能③ 研报 / 机构评级（东财 reportapi.eastmoney.com/report/list）。

实测字段说明（2026-09-27 复验）：
  · 评级：emRatingName（"买入"/"增持"/"中性"/"减持"/"卖出"，少数会有"强烈推荐"等变体）
  · 目标价：indvAimPriceT —— 真网络抽样中绝大多数公司动态研报 **目标价为空字符串**，
    这是券商新版（2024 起）业内趋势：不再硬填目标价，改用 EPS/PE 替代估值。
    因此本模块在返回 rows 时同时带 has_target / eps / pe 字段，前端既显示目标价
    又给出 EPS/PE 兜底，并允许用户切到"仅看带目标价的研报"。
"""
from __future__ import annotations
import datetime
import emapi

# 东财各类评级名 → 五档归一（兜底用，emRatingName 通常已是标准五档）
_RATING_MAP = [
    (("买入", "强烈推荐", "推荐", "强烈买入"), "buy"),
    (("增持", "审慎推荐", "跑赢行业", "优于大市", "谨慎推荐", "强于大市"), "overweight"),
    (("中性", "持有", "同步大市", "中性评级", "标配"), "neutral"),
    (("减持", "弱于大市", "谨慎", "跑输大市", "跑输行业", "弱于行业"), "underweight"),
    (("卖出",), "sell"),
]
_RATING_LABEL = {"buy": "买入", "overweight": "增持", "neutral": "中性",
                 "underweight": "减持", "sell": "卖出"}

# 2026-10-01 口径自查修复：模糊匹配必须「长词优先」。
# 原实现按表顺序逐词 `kw in s`，导致短词抢走长词：
#   "审慎推荐（维持）" 被 "推荐" 抢成 buy（应为 overweight）
#   "谨慎推荐"         被 "推荐" 抢成 buy（应为 overweight）
# 展开成扁平列表后按 (词长倒序, 表内顺序) 排序，长词先命中。
_FUZZY = [(kw, k) for keys, k in _RATING_MAP for kw in keys]
_FUZZY.sort(key=lambda t: (-len(t[0]), _RATING_MAP.index(
    next(x for x in _RATING_MAP if t[0] in x[0]))))


def _norm_rating(name: str):
    s = (name or "").strip()
    if not s:
        return None
    # 先尝试直接命中标准档
    std = {"买入": "buy", "增持": "overweight", "中性": "neutral", "减持": "underweight", "卖出": "sell",
           "强烈推荐": "buy", "推荐": "buy", "强烈买入": "buy", "审慎推荐": "overweight",
           "持有": "neutral", "减持": "underweight",
           # "持有" 与 "谨慎" 同为 2 字，模糊匹配会撞车；复合评级显式指认优先
           "谨慎持有": "underweight"}
    if s in std:
        return std[s]
    # 模糊匹配（长词优先，见 _FUZZY 排序说明）
    for kw, k in _FUZZY:
        if kw in s:
            return k
    return None


def _url(it: dict) -> str:
    eu = (it.get("encodeUrl") or "").strip()
    if eu.startswith("http"):
        return eu
    if eu.startswith("/"):
        return "https://data.eastmoney.com" + eu
    code = it.get("infoCode") or eu
    return ("https://data.eastmoney.com/report/info/%s.html" % code) if code else ""


def _row(it: dict) -> dict:
    target = emapi._num(it.get("indvAimPriceT"))  # None 表示 "—" 或空字符串
    pe = emapi._num(it.get("predictThisYearPe"))
    eps = emapi._num(it.get("predictThisYearEps"))
    return {
        "title": it.get("title") or "",
        "org": it.get("orgSName") or it.get("orgName") or "",
        "rating": it.get("emRatingName") or it.get("sRatingName") or "",
        "rating_code": _norm_rating(it.get("emRatingName") or it.get("sRatingName")),
        "target": target,
        "has_target": target is not None,
        "pe": pe,           # 预测当年 PE（业内替代目标价的口径）
        "eps": eps,         # 预测当年 EPS
        "date": str(it.get("publishDate") or "")[:10],
        "stock": it.get("stockName") or "",
        "stock_code": it.get("stockCode") or "",
        "url": _url(it),
    }


def report_list(pz: int = 30, page: int = 1) -> dict:
    r = emapi.report_list(page_size=pz, page_no=page)
    if not r["ok"]:
        return {"ok": False, "rows": [], "reason": r.get("err")}
    rows = [_row(x) for x in r["rows"]]
    has_t = sum(1 for x in rows if x["has_target"])
    return {"ok": True, "rows": rows, "hits": r.get("hits", 0),
            "n_with_target": has_t, "n_total": len(rows)}


def rating(symbol: str) -> dict:
    code = "".join(ch for ch in (symbol or "") if ch.isdigit())
    if not code:
        return {"ok": False, "symbol": symbol, "reason": "请输入 A 股数字代码"}
    begin = (datetime.date.today() - datetime.timedelta(days=180)).strftime("%Y-%m-%d")
    r = emapi.report_list(page_size=100, page_no=1, begin_time=begin, code=code)
    if not r["ok"]:
        return {"ok": False, "symbol": symbol, "reason": r.get("err")}
    rows_raw = r["rows"]
    rows = [_row(x) for x in rows_raw]
    dist = {"buy": 0, "overweight": 0, "neutral": 0, "underweight": 0, "sell": 0}
    targets = []
    pes = []
    for x in rows:
        k = x["rating_code"]
        if k:
            dist[k] += 1
        if x["has_target"]:
            targets.append(x["target"])
        if x["pe"] is not None:
            pes.append(x["pe"])
    top = max(dist, key=lambda k: dist[k]) if any(dist.values()) else None
    return {
        "ok": True, "symbol": symbol, "code": code,
        "total_orgs": len(rows),
        "n_with_target": sum(1 for x in rows if x["has_target"]),
        "n_with_pe": len(pes),
        "dist": dist,
        "target": round(sum(targets) / len(targets), 2) if targets else None,
        "pe_avg": round(sum(pes) / len(pes), 1) if pes else None,
        "price": None,
        "rating": _RATING_LABEL.get(top, "—") if top else "—",
    }
