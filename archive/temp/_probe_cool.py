# -*- coding: utf-8 -*-
"""临时探针5：等东财冷却后单次验证分钟曲线 + 大盘资金流汇总（用完删）。"""
import json, sys, time, urllib.request, urllib.parse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
UT = "fa5fd1943c7b386f172d6893dbfba10b"


def get(url, ref="https://quote.eastmoney.com/", timeout=12):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref}), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"__err": f"{type(e).__name__}: {str(e)[:100]}"}


print("冷却 75s 后单次测试（验证「低频可用」假设）...", flush=True)
time.sleep(75)

print("\n=== 1) 单次：板块分钟资金流（曲线核心）===")
u = (f"https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=90.BK1264"
     f"&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
     f"&klt=1&lmt=0&ut={UT}&_={int(time.time()*1000)}")
j = get(u)
if "__err" in j:
    print("   [push2] FAIL", j["__err"])
else:
    d = j.get("data") or {}
    kl = d.get("klines") or []
    print(f"   [push2] rc={j.get('rc')} name={d.get('name')} 分钟点数={len(kl)}")
    if kl:
        print("     首:", kl[0])
        print("     末:", kl[-1])

print("\n=== 2) 大盘资金流汇总（截图1 顶部那排：主力/超大/大单/中单/小单）===")
for sec, tag in (("1.000001", "上证"), ("0.399001", "深证"), ("1.000001,0.399001", "沪+深")):
    u2 = (f"https://push2.eastmoney.com/api/qt/ulist.np/get?secids={sec}"
          f"&fields=f2,f3,f12,f14,f62,f66,f72,f78,f84,f184&ut={UT}&fltt=2&invt=2&_={int(time.time()*1000)}")
    j2 = get(u2)
    print(f"   [{tag}] ", json.dumps(j2, ensure_ascii=False)[:300] if "__err" not in j2 else j2["__err"])

print("\n=== 3) 东财「资金流向」页大盘实时汇总（web 口）===")
for path, ref in (("/api/qt/stock/fflow/kline/get?secid=1.000001&fields1=f1,f2,f3,f7"
                   "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65&klt=1&lmt=0", "https://quote.eastmoney.com/"),):
    j3 = get(f"https://push2.eastmoney.com{path}&ut={UT}&_={int(time.time()*1000)}", ref)
    if "__err" in j3:
        print("   FAIL", j3["__err"])
    else:
        d = j3.get("data") or {}
        kl = d.get("klines") or []
        print(f"   上证指数 klt=1 -> 点数={len(kl)} name={d.get('name')}")
        if kl:
            print("     末:", kl[-1])

print("\n=== 4) 新浪板块资金流 行业全量（兜底源，确认能取全）===")
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
u4 = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
      f"MoneyFlow.ssl_bkzj_bk?page=1&num=60&sort=netamount&asc=0&fenlei=1&_={int(time.time()*1000)}")
try:
    with op.open(urllib.request.Request(u4, headers={"User-Agent": UA,
              "Referer": "https://finance.sina.com.cn/"}), timeout=12) as r:
        arr = json.loads(r.read().decode("utf-8", "replace"))
    print(f"   行业板块 {len(arr)} 条")
    for r in arr[:5]:
        print(f"     {r.get('name')} netamount={r.get('netamount')} ratio={r.get('ratioamount')} chg={r.get('avg_changeratio')}")
except Exception as e:
    print("   FAIL", e)
