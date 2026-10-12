# -*- coding: utf-8 -*-
"""临时探针3：为板块资金流找可用数据源（东财多前缀 / 新浪 / 同花顺 / 数据中心）（用完删）。"""
import json, ssl, sys, time, urllib.request, urllib.parse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
UT = "fa5fd1943c7b386f172d6893dbfba10b"


def raw(url, ref="https://data.eastmoney.com/", timeout=10, extra=None):
    h = {"User-Agent": UA, "Referer": ref}
    if extra:
        h.update(extra)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            return r.getcode(), r.read().decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"


EMQ = ("/api/qt/clist/get?fid=f62&po=1&pz=3&pn=1&np=1&fltt=2&invt=2"
       "&fs=" + urllib.parse.quote("m:90+t:2+f:!50") + "&fields=f12,f14,f3,f62&ut=" + UT)

print("=" * 72)
print("1) 东财 push2 数字前缀镜像（换 IP 绕风控）")
print("=" * 72)
for pre in ("1", "7", "17", "33", "66", "82", "96"):
    h = f"{pre}.push2.eastmoney.com"
    code, txt = raw(f"https://{h}{EMQ}")
    ok = txt.startswith("{") and '"diff"' in txt
    print(f"  {h:32s} code={code} {'OK 有数据' if ok else txt[:90]}")
    if ok:
        break

print()
print("=" * 72)
print("2) 东财 datacenter-web 板块资金流报表（200 可达的那个主机）")
print("=" * 72)
for rpt in ("RPT_DMSK_BKZJLX", "RPT_BKZJLX", "RPT_MUTUAL_BKZJLX"):
    q = urllib.parse.urlencode({"reportName": rpt, "columns": "ALL", "pageSize": 5, "pageNumber": 1,
                                "sortColumns": "NET_AMOUNT", "sortTypes": -1, "source": "WEB", "client": "WEB"})
    code, txt = raw(f"https://datacenter-web.eastmoney.com/api/data/v1/get?{q}")
    print(f"  {rpt:22s} code={code} {txt[:170]}")

print()
print("=" * 72)
print("3) 新浪 板块资金流（MoneyFlow.ssl_bkzj_bk）")
print("=" * 72)
for fenlei, tag in (("1", "行业"), ("0", "概念")):
    u = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         f"MoneyFlow.ssl_bkzj_bk?page=1&num=5&sort=netamount&asc=0&fenlei={fenlei}&_={int(time.time()*1000)}")
    code, txt = raw(u, "https://finance.sina.com.cn/")
    print(f"  [{tag}] code={code} {txt[:220]}")

print()
print("=" * 72)
print("4) 同花顺 板块资金流")
print("=" * 72)
for u, ref in (
    ("http://data.10jqka.com.cn/funds/hyzjl/field/tradezdf/order/desc/page/1/ajax/1/free/1/", "http://data.10jqka.com.cn/funds/hyzjl/"),
    ("http://d.10jqka.com.cn/v6/line/bk_881121/01/last.js", "http://q.10jqka.com.cn/"),
):
    code, txt = raw(u, ref, extra={"X-Requested-With": "XMLHttpRequest"})
    print(f"  {u[:58]:58s} code={code} {txt[:120]}")

print()
print("=" * 72)
print("5) 东财 push2 板块快照（再确认一次是否已恢复）")
print("=" * 72)
code, txt = raw(f"https://push2.eastmoney.com{EMQ}")
print(f"  push2: code={code} {txt[:150]}")
