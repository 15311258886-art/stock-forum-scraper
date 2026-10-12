# -*- coding: utf-8 -*-
"""临时探针4：板块分钟级资金流（截图1 曲线数据源）可用性 + 字段序（用完删）。"""
import json, sys, time, urllib.request, urllib.parse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
UT = "fa5fd1943c7b386f172d6893dbfba10b"


def get(url, ref="https://quote.eastmoney.com/", timeout=12):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref}), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print("   FAIL", type(e).__name__, str(e)[:110])
        return None


print("=" * 72)
print("1) 板块分钟资金流 fflow/kline klt=1 —— 多主机")
print("=" * 72)
for bk in ("BK1264", "BK0447", "BK0493"):
    for host in ("push2", "1.push2", "push2his"):
        u = (f"https://{host}.eastmoney.com/api/qt/stock/fflow/kline/get?secid=90.{bk}"
             f"&fields1=f1,f2,f3,f7"
             f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
             f"&klt=1&lmt=0&ut={UT}&_={int(time.time()*1000)}")
        j = get(u)
        if not j:
            print(f"  [{bk} {host}] 无返回")
            continue
        d = j.get("data") or {}
        kl = d.get("klines") or []
        print(f"  [{bk} {host}] rc={j.get('rc')} name={d.get('name')} klines={len(kl)}")
        if kl:
            print("       首:", kl[0])
            print("       末:", kl[-1])
        break  # 每只板块只试第一个通的

print()
print("=" * 72)
print("2) 同一接口 klt=101（日线）确认字段序一致")
print("=" * 72)
u = (f"https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=90.BK1264"
     f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
     f"&klt=101&lmt=3&ut={UT}&_={int(time.time()*1000)}")
j = get(u)
if j:
    for line in ((j.get("data") or {}).get("klines") or [])[-3:]:
        print("   ", line)

print()
print("=" * 72)
print("3) 板块列表（前 40，用于挑曲线对象）")
print("=" * 72)
u = ("https://1.push2.eastmoney.com/api/qt/clist/get?fid=f62&po=1&pz=40&pn=1&np=1&fltt=2&invt=2"
     "&fs=" + urllib.parse.quote("m:90+t:2+f:!50") + "&fields=f12,f14,f3,f62,f184&ut=" + UT
     + "&_=" + str(int(time.time() * 1000)))
j = get(u, "https://data.eastmoney.com/zjlx/list.html")
if j:
    diff = ((j.get("data") or {}).get("diff")) or []
    if isinstance(diff, dict):
        diff = [diff[k] for k in sorted(diff.keys(), key=lambda x: int(x) if str(x).isdigit() else x)]
    print(f"   共 {len(diff)} 条，前 8:")
    for r in diff[:8]:
        print("    ", r)

print()
print("=" * 72)
print("4) 新浪板块资金流 完整字段（兜底源的字段名）")
print("=" * 72)
u = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
     f"MoneyFlow.ssl_bkzj_bk?page=1&num=3&sort=netamount&asc=0&fenlei=1&_={int(time.time()*1000)}")
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with op.open(urllib.request.Request(u, headers={"User-Agent": UA,
                  "Referer": "https://finance.sina.com.cn/"}), timeout=12) as r:
        arr = json.loads(r.read().decode("utf-8", "replace"))
    print("   条数:", len(arr), "字段:", list(arr[0].keys()) if arr else "-")
    for r in arr[:2]:
        print("    ", json.dumps(r, ensure_ascii=False)[:260])
except Exception as e:
    print("   FAIL", e)
