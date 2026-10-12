#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回测·实时链路计时与当日可得性验证（只读，不写库）

测四件事：
  A. 今天(交易日)的龙虎榜能不能取到、几点可取
  B. 各抓取源单次请求耗时与成功率（判断盘中刷新是否跟得上）
  C. 行情/个股接口（查看器点开要看的那几个）端到端耗时
  D. 市场数据源在“盘中密集请求”下是否会限流
"""
import os
import ssl
import sys
import time
import json
import urllib.request
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "viewer"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                 urllib.request.HTTPSHandler(context=CTX))
UT = "7eea3edcaed734bea9cbfc24409ed989"


def timed(label, fn):
    t0 = time.time()
    try:
        r = fn()
        dt = (time.time() - t0) * 1000
        return {"label": label, "ok": True, "ms": round(dt), "val": r}
    except Exception as e:  # noqa: BLE001
        dt = (time.time() - t0) * 1000
        return {"label": label, "ok": False, "ms": round(dt),
                "val": f"{type(e).__name__}: {str(e)[:90]}"}


def get_json(url, ref="https://data.eastmoney.com/", timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref})
    with OP.open(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8-sig", "ignore")
    return json.loads(raw)


def dc(report, flt="", size=3, sort="", source="WEB", client="WEB"):
    p = {
        "reportName": report, "columns": "ALL", "pageSize": str(size),
        "pageNumber": "1", "source": source, "client": client, "ut": UT,
    }
    if flt:
        p["filter"] = flt
    if sort:
        p["sortColumns"], p["sortTypes"] = sort.split(",")
    return "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(p)


def main():
    today = time.strftime("%Y-%m-%d")
    now = time.strftime("%H:%M:%S")
    print("=" * 72)
    print(f"[回测时刻] {today} {now}  (周五=交易日)")
    print("=" * 72)

    # ---------- A. 当日龙虎榜可得性 ----------
    print()
    print("【A】当日龙虎榜可得性（T 日盘后披露，决定时效上限）")
    for d in [today]:
        # 看当日有没有数据
        r = timed(f"LHB个股 {d}", lambda d=d: (
            lambda j: ((j.get("result") or {}).get("count") or 0,
                       len(((j.get("result") or {}).get("data") or [])))
        )(get_json(dc("RPT_DAILYBILLBOARD_DETAILSNEW", f"(TRADE_DATE='{d}')", 3))))
        print(f"   {r['label']}: {'OK' if r['ok'] else 'X'}  {r['ms']}ms  "
              f"total/rows={r['val']}")
    # 看最新的交易日是哪天
    r2 = timed("LHB个股 最近(无日期过滤)", lambda: (
        lambda j: (((j.get("result") or {}).get("data") or [{}]))[0].get("TRADE_DATE")
    )(get_json(dc("RPT_DAILYBILLBOARD_DETAILSNEW", "", 1,
                  "TRADE_DATE,-1"))))
    print(f"   最新可用交易日: {r2['val']}  ({r2['ms']}ms)")
    # 席位
    r3 = timed("LHB席位 当日", lambda d=today: (
        lambda j: ((j.get("result") or {}).get("count") or 0)
    )(get_json(dc("RPT_BILLBOARD_TRADEDETAIL", f"(TRADE_DATE='{d}')", 1))))
    print(f"   席位当日 total={r3['val']}  ({r3['ms']}ms)")

    # ---------- B. 各源单次抓取耗时 ----------
    print()
    print("【B】各抓取源实时性（单次请求耗时；判断盘中能否 10 分钟刷新一轮）")
    tests = []

    def _sina():
        u = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=120")
        req = urllib.request.Request(u, headers={"User-Agent": UA,
                                                 "Referer": "https://finance.sina.com.cn/"})
        with OP.open(req, timeout=20) as r:
            return len(json.loads(r.read().decode("utf-8", "ignore")))

    def _tencent():
        u = "https://qt.gtimg.cn/q=sh600519"
        req = urllib.request.Request(u, headers={"User-Agent": UA,
                                                 "Referer": "https://gu.qq.com/"})
        with OP.open(req, timeout=15) as r:
            return len(r.read())

    def _em_quote():
        u = ("https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519"
             f"&fields=f57,f58,f43,f170&ut={UT}&invt=2&fltt=2")
        return get_json(u, "https://quote.eastmoney.com/").get("data", {}).get("f43")

    def _em_f10():
        return get_json(dc("RPT_F10_FINANCE_MAINFINADATA", '(SECUCODE="600519.SH")',
                           1, "REPORT_DATE,-1", "HSF10", "PC"),
                        "https://emweb.securities.eastmoney.com/")

    def _xueqiu_home():
        req = urllib.request.Request("https://xueqiu.com/",
                                     headers={"User-Agent": UA})
        with OP.open(req, timeout=20) as r:
            return len(r.read())

    for lbl, fn in [("新浪K线(全历史源)", _sina), ("腾讯行情(兜底)", _tencent),
                    ("东财push2实时行情", _em_quote), ("东财F10财务", _em_f10),
                    ("雪球首页", _xueqiu_home)]:
        r = timed(lbl, fn)
        print(f"   {lbl:<20} {'OK ' if r['ok'] else 'FAIL'} {r['ms']:>6}ms  "
              f"{str(r['val'])[:70]}")

    # ---------- C. 查看器接口端到端 ----------
    print()
    print("【C】查看器接口端到端（用户点开会感知的延迟）")
    import importlib
    sys.path.insert(0, os.path.join(BASE, "viewer"))
    os.chdir(os.path.join(BASE, "viewer"))
    try:
        import server as S
        importlib.reload(S)
        for lbl, fn in [
            ("/api/quote 600519", lambda: len(json.dumps(S.api_quote("600519"), ensure_ascii=False))),
            ("/api/kline day", lambda: S.api_kline("600519", "day").get("count")),
            ("/api/kline week", lambda: S.api_kline("600519", "week").get("count")),
            ("/api/lhb", lambda: len((S.api_lhb() or {}).get("stocks") or [])),
            ("/api/lhb_sectors", lambda: len((S.api_lhb_sectors() or {}).get("sectors") or [])),
            ("/api/players", lambda: len(S.api_players() if isinstance(S.api_players(), list) else [])),
        ]:
            r = timed(lbl, fn)
            print(f"   {lbl:<20} {'OK ' if r['ok'] else 'FAIL'} {r['ms']:>6}ms  {r['val']}")
    except Exception as e:  # noqa: BLE001
        print("   接口计时失败:", type(e).__name__, e)

    # ---------- D. 限流敏感性 ----------
    print()
    print("【D】密集请求敏感性（模拟盘中连续刷新，看是否触发限流）")
    n_ok = 0
    n_fail = 0
    fails = []
    for i in range(8):
        r = timed(f"push2-{i}", _em_quote)
        if r["ok"]:
            n_ok += 1
        else:
            n_fail += 1
            fails.append(str(r["val"])[:60])
        time.sleep(0.3)
    print(f"   东财push2 连打 8 次: 成功 {n_ok} / 失败 {n_fail}")
    if fails:
        print(f"   失败样例: {fails[:3]}")
    n_ok2 = n_fail2 = 0
    fails2 = []
    for i in range(8):
        r = timed(f"sina-{i}", _sina)
        if r["ok"]:
            n_ok2 += 1
        else:
            n_fail2 += 1
            fails2.append(str(r["val"])[:60])
        time.sleep(0.2)
    print(f"   新浪接口 连打 8 次: 成功 {n_ok2} / 失败 {n_fail2}")
    if fails2:
        print(f"   失败样例: {fails2[:3]}")

    print()
    print("[回测完成]")


if __name__ == "__main__":
    main()
