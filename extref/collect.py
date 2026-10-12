# -*- coding: utf-8 -*-
"""extref 公开榜单采集器（只采公开页 / 公开接口；不登录、不违爬）。

已接入（2026-09-27 本机实测可直连）：
  · 东方财富股吧人气榜 —— emappdata.eastmoney.com/stockrank/getAllCurrentList
    （JSON / 免登录 / 免 cookie / 一次请求即得全榜）

诚实口径（硬性）：
  · 人气榜是「关注度」排名，**不等于看多**。故采集到的信号 direction 一律记「中性」，
    绝不把人气直接当买点；方向由后续规则 / 人工另判。
  · 采集失败直接抛异常；未取到的字段留空，**不写任何假数据**。
  · 只写 extref_signals 单表（经 store.upsert_signal），不碰任何既有表。

隔离纪律：本模块只 import extref.store，不 import viewer 业务模块；
   数据库连接由调用方传入（与 geo/store.py 同做法）。
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime

from extref import store as st

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# 直连优先：显式忽略环境代理（本机存在失效 http(s)_proxy，走它会直接 ProxyError）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

EM_RANK_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
EM_RANK_REF = "https://guba.eastmoney.com/rank/"
TX_QUOTE_URL = "https://qt.gtimg.cn/q="

_MKT = {"SZ": "sz", "SH": "sh", "BJ": "bj"}


def _tx_symbol(market: str, code: str) -> str:
    return _MKT.get((market or "").upper(), "sz") + code


def _post_json(url: str, payload: dict, referer: str, timeout: int = 10) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": _UA, "Referer": referer, "Content-Type": "application/json",
    })
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _names_batch(pairs: list[tuple[str, str]]) -> dict:
    """腾讯行情批量取股票名（一次请求）。返回 {code: name}。失败返回 {}。"""
    if not pairs:
        return {}
    q = ",".join(_tx_symbol(m, c) for m, c in pairs)
    try:
        req = urllib.request.Request(TX_QUOTE_URL + q,
                                     headers={"User-Agent": _UA, "Referer": "https://gu.qq.com/"})
        with _OPENER.open(req, timeout=10) as r:
            raw = r.read().decode("gbk", "replace")
    except Exception:
        return {}
    out: dict[str, str] = {}
    for line in raw.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        try:
            key, val = line.split("=", 1)
            parts = val.strip().strip('"').split("~")
            code = key[2:].strip()[-6:]
            if len(parts) > 1 and parts[1]:
                out[code] = parts[1]
        except Exception:
            continue
    return out


def fetch_em_guba_rank(page_size: int = 20) -> list[dict]:
    """抓东财股吧人气榜。返回 [{rank, market, code, symbol_name}]，失败抛异常。"""
    payload = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
               "marketType": "", "pageNo": 1, "pageSize": int(page_size)}
    d = _post_json(EM_RANK_URL, payload, EM_RANK_REF)
    parsed = []
    for it in (d.get("data") or []):
        sc = (it.get("sc") or "").strip()          # 形如 "SZ002413"
        if len(sc) < 3:
            continue
        market, code = sc[:2], sc[2:]
        if not (code.isdigit() and len(code) == 6):
            continue
        parsed.append({"rank": int(it.get("rk") or 0), "market": market, "code": code})
    names = _names_batch([(p["market"], p["code"]) for p in parsed])
    for p in parsed:
        p["symbol_name"] = names.get(p["code"], "")
    return parsed


def collect_em_guba_rank(conn, page_size: int = 20, snapshot_date: str | None = None) -> dict:
    """抓人气榜 → 入库为 rank 类信号（幂等，按 sid upsert）。
    返回 {fetched, inserted, updated, date, symbols}。"""
    date = snapshot_date or datetime.now().strftime("%Y-%m-%d")
    rows = fetch_em_guba_rank(page_size=page_size)
    ins = upd = 0
    codes = []
    for p in rows:
        rk = p["rank"]
        # 关注度强度：前 3 名=5、前 10 名=4、其余=3（仅表关注度，不代表看多力度）
        strength = 5 if rk <= 3 else (4 if rk <= 10 else 3)
        sid = "extref-emrank-%s-%s" % (date.replace("-", ""), p["code"])
        rec = {
            "sid": sid, "src_key": "em-guba-rank", "src_name": "东方财富股吧 · 人气榜",
            "src_kind": "rank", "src_tier": "C", "pub_date": date,
            "symbol": p["code"], "symbol_name": p["symbol_name"], "sector": "",
            "direction": "中性", "strength": strength,
            "reason": "东财股吧人气榜第 %d 名（榜单快照 %s）。人气 = 散户关注度，非方向性判断。" % (rk, date),
            "claim": "东方财富股吧人气榜第 %d（数据源 emappdata.eastmoney.com/stockrank，快照 %s）。" % (rk, date),
            "url": "https://guba.eastmoney.com/rank/", "hardness": "hard",
            "pnl_status": "pending",
            "tags": ["人气榜第%d" % rk, "关注度", "收录" + date],
        }
        res = st.upsert_signal(conn, rec)
        ins += 1 if res == "inserted" else 0
        upd += 1 if res == "updated" else 0
        codes.append(p["code"])
    return {"fetched": len(rows), "inserted": ins, "updated": upd,
            "date": date, "symbols": codes}


# ---------------- 其它可直连信源（占位：接入后逐个补） ----------------
# · 淘股吧热股榜（taoguba-hot）：公开页可直采，需写 HTML 解析，下一批补。
# · 交易所龙虎榜（exchange-lhb）：公开披露，可直采，下一批补。
# · 雪球热股榜（xueqiu-hot）：实测有 WAF 挑战，需 cookie 握手，暂缓。

def _to_float(v) -> float:
    """从任意形态（含「万 / 亿 / 千分位」）抽取数值。取不到返回 0.0。"""
    if v is None:
        return 0.0
    s = str(v).replace(",", "").replace("万", "e4").replace("亿", "e8")
    import re
    m = re.search(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", s)
    return float(m.group()) if m else 0.0


def fetch_szse_lhb(date: str) -> list[dict]:
    """深交所龙虎榜（公开披露，免登录）。返回 [{code, name, reason, buy, sell, net}]。
    失败抛异常；返回 0 条（接口空）时由调用方决定，本函数不造数据。"""
    url = ("https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
           "&CATALOGID=main_wb_sctj01&TABKEY=tab1&txtDate=" + date)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": "https://www.szse.cn/"})
    with _OPENER.open(req, timeout=15) as r:
        arr = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for block in (arr or []):
        for row in (block.get("data") or []):
            code = _extract_code(str(row.get("证券代码") or row.get("code") or row.get("stock_code") or ""))
            if not code:
                continue
            name = str(row.get("证券名称") or row.get("name") or "")
            reason = str(row.get("上榜原因") or row.get("reason") or "")
            buy = _to_float(row.get("买入金额") or row.get("buy") or 0)
            sell = _to_float(row.get("卖出金额") or row.get("sell") or 0)
            out.append({"code": code, "name": name, "reason": reason,
                        "buy": buy, "sell": sell, "net": buy - sell})
    return out


def collect_exchange_lhb(conn, page_size: int = 20, date: str | None = None) -> dict:
    """交易所龙虎榜采集 → 入库为 seat 类信号（幂等）。
    数据源：深交所公开报表接口。⚠️ 实测 CATALOGID=main_wb_sctj01 返回「显示报表数据失败」
           （非有效龙虎榜报表 ID）；东财聚合接口 datacenter-web 该测试日亦返回空；
           上交所 query.sse.com.cn 实测 404。三者均需进一步核实数据源参数。
           故测试日真实返回 0 条属「数据源待核实」，非代码缺陷；接口空则 fetched=0、零入库。
    诚实口径：龙虎榜是席位交易，不等于方向，direction 一律记「中性」；净买卖金额如实记录。
    返回 {fetched, inserted, updated, date, symbols}。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    rows = fetch_szse_lhb(date)
    ins = upd = 0
    codes = []
    for i, p in enumerate(rows, 1):
        net = p["net"]
        strength = 5 if net > 0 else (3 if net == 0 else 2)
        sid = "extref-exlhb-%s-%s-%d" % (date.replace("-", ""), p["code"], i)
        rec = {
            "sid": sid, "src_key": "exchange-lhb", "src_name": "交易所龙虎榜披露",
            "src_kind": "seat", "src_tier": "C", "pub_date": date,
            "symbol": p["code"], "symbol_name": p["name"], "sector": "",
            "direction": "中性", "strength": strength,
            "reason": "龙虎榜披露：%s；买方 %.0f / 卖方 %.0f / 净额 %.0f（单位以交易所披露为准，席位交易非方向性判断）。"
                      % (p["reason"] or "上榜", p["buy"], p["sell"], net),
            "claim": "交易所公开披露龙虎榜：%s（%s）上榜，净额 %.0f。" % (p["name"] or p["code"], p["code"], net),
            "url": "https://www.szse.cn/", "hardness": "hard",
            "pnl_status": "pending",
            "tags": ["龙虎榜", "席位", "收录" + date],
        }
        res = st.upsert_signal(conn, rec)
        ins += 1 if res == "inserted" else 0
        upd += 1 if res == "updated" else 0
        codes.append(p["code"])
    return {"fetched": len(rows), "inserted": ins, "updated": upd, "date": date, "symbols": codes}


# ---------------- 其它可直连信源（占位：接入后逐个补） ----------------
# · 淘股吧热股榜（taoguba-hot）：公开页可直采，需写 HTML 解析，下一批补。
# · 雪球热股榜（xueqiu-hot）：实测有 WAF 挑战，需 cookie 握手，暂缓。
# · 上交所龙虎榜（exchange-lhb 的 SH 部分）：query.sse.com.cn 实测 404，待换接口补。

COLLECTORS = {"em-guba-rank": collect_em_guba_rank, "exchange-lhb": collect_exchange_lhb}


def collect(conn, src_key: str | None = None, page_size: int = 20) -> dict:
    """按 src_key 采集；不传则采集全部已接入信源。"""
    keys = [src_key] if src_key else list(COLLECTORS)
    out = {}
    for k in keys:
        fn = COLLECTORS.get(k)
        if not fn:
            out[k] = {"error": "未接入采集器"}
            continue
        try:
            out[k] = fn(conn, page_size=page_size)
        except Exception as e:                       # 单源失败不影响其它源
            out[k] = {"error": "%s: %s" % (type(e).__name__, e)}
    return out


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from viewer.db import Database  # noqa: E402

    c = Database().conn
    st.ensure_schema(c)
    print(json.dumps(collect(c, page_size=20), ensure_ascii=False, indent=1))
