# -*- coding: utf-8 -*-
"""增强功能③ 估值诊股（透明规则评分，东财「价值分析」报表）。

重要：RPT_VALUEANALYSIS_DET **无 ROE / 股息率 / 盈利增速**字段，故评分只用
报表真实存在的估值因子：PE_TTM / PB_MRQ / PS_TTM / PEG_CAR / PCF_OCF_TTM。
评分是「规则打分」，非券商研报，不构成投资建议。
"""
from __future__ import annotations
import re
import emapi

_DISCLAIMER = ("规则评分：按 PE/PB/PS/PEG/现金流 五因子加权换算 0–100，"
               "分越高代表估值越便宜/越具吸引力。非券商研报，不构成投资建议。")


def _lin(v, best, worst):
    """v 越接近 best 越接近 100；越接近/超过 worst 越接近 0。v 为 None 返回 None。"""
    if v is None:
        return None
    if v <= best:
        return 100
    if v >= worst:
        return 0
    return round(100 * (worst - v) / (worst - best))


def _components(pe, pb, ps, peg, pcf):
    return [
        ("市盈率 PE", _lin(pe, 10, 50) if (pe or 0) > 0 else None, 0.30),
        ("市净率 PB", _lin(pb, 1, 8) if (pb or 0) > 0 else None, 0.25),
        ("市销率 PS", _lin(ps, 1, 8) if (ps or 0) > 0 else None, 0.15),
        ("PEG", _lin(peg, 0.5, 3) if (peg or 0) > 0 else None, 0.20),
        ("现金流 PCF", _lin(pcf, 8, 40) if (pcf or 0) > 0 else None, 0.10),
    ]


def _verdict(score):
    if score >= 80:
        return "估值偏低 · 具吸引力"
    if score >= 60:
        return "估值合理 · 略偏便宜"
    if score >= 40:
        return "估值中性"
    return "估值偏高 · 需谨慎"


def score(symbol: str) -> dict:
    code = re.sub(r"\D", "", symbol or "")
    if not code:
        return {"ok": False, "symbol": symbol,
                "reason": "该代码非 A 股数字代码，估值报表仅覆盖沪深京 A 股"}
    r = emapi.datacenter("RPT_VALUEANALYSIS_DET", flt='(SECURITY_CODE="%s")' % code,
                         sort_columns="TRADE_DATE", sort_types="-1", page_size=1)
    if not r["ok"] or not r["rows"]:
        return {"ok": False, "symbol": symbol,
                "reason": "估值数据取不到（%s）" % (r.get("err") or "无数据")}
    x = r["rows"][0]
    pe, pb, ps = emapi._num(x.get("PE_TTM")), emapi._num(x.get("PB_MRQ")), emapi._num(x.get("PS_TTM"))
    peg, pcf = emapi._num(x.get("PEG_CAR")), emapi._num(x.get("PCF_OCF_TTM"))
    comps_raw = _components(pe, pb, ps, peg, pcf)
    avail = [(lbl, sc, w) for (lbl, sc, w) in comps_raw if sc is not None]
    if avail:
        tot_w = sum(w for _, _, w in avail)
        total = round(sum(sc * w for _, sc, w in avail) / tot_w)
    else:
        total = 0
    comps = [{"label": lbl, "score": sc} for (lbl, sc, _w) in comps_raw]
    return {
        "ok": True, "symbol": symbol, "code": x.get("SECURITY_CODE"),
        "name": x.get("SECURITY_NAME_ABBR"), "industry": x.get("BOARD_NAME") or "",
        "pe": pe, "pb": pb, "ps": ps, "peg": peg, "pcf": pcf,
        "pct": emapi._num(x.get("CHANGE_RATE")),
        "close": emapi._num(x.get("CLOSE_PRICE")),
        "trade_date": str(x.get("TRADE_DATE") or "")[:10],
        "score": total, "verdict": _verdict(total),
        "components": comps, "disclaimer": _DISCLAIMER, "reason": None,
    }
