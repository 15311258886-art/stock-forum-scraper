"""探活：寻找能通的资金流排行/个股资金流替代接口。"""
from __future__ import annotations
import urllib.request, urllib.parse, json, socket, sys, time

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(200_000)
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000) if hasattr(e, "read") else b""
    except Exception as e:
        return None, str(e).encode()

# ========== 候选1：datacenter-web 主力净流入排行（多条报表名）============
DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORTS = [
    # 报告名                排序列                  filte
    ("RPT_MUTUAL_RANK",         "f2", "f2"),     # 主力净流入排行
    ("RPT_MUTUAL_RANK_DET",     "f2", "f2"),     # 主力净流入排行(含个股)
    ("RPT_MUTUAL_STOCK_HOLDRANKS","f1","f1"),    # 主力持仓变化
    ("RPT_F10_MAIN_FORECAST",   "f3", "f3"),     # 主力预测？不一定
]
print("=" * 60)
print("候选1：datacenter-web 报表")
for rn, sc, ft in REPORTS:
    qs = urllib.parse.urlencode({
        "reportName": rn, "columns": "ALL", "pageNumber": 1, "pageSize": 5,
        "sortColumns": sc, "sortTypes": "-1",
        "source": "WEB", "client": "WEB", "p": 1, "pageNo": 1, "pageNum": 1,
        "_": int(time.time()*1000),
    })
    st, b = get(f"{DC}?{qs}")
    txt = b[:500].decode("utf-8", "ignore") if isinstance(b, bytes) else str(b)
    print(f"\n[{rn}] -> HTTP {st}")
    if st == 200 and txt and '"success":true' in txt:
        # 抽一个字段看
        try:
            d = json.loads(b)
            rows = (d.get("result") or {}).get("data") or []
            print(f"   ✓ 成功，rows={len(rows)}")
            if rows: print(f"     首行: {json.dumps(rows[0], ensure_ascii=False)[:300]}")
        except Exception as e:
            print(f"   parse err {e}: {txt[:200]}")
    else:
        print(f"   ✗ {txt[:200]}")

# ========== 候选2：push2his 历史接口（h5/old）============
print("\n" + "=" * 60)
print("候选2：push2his / 老 push2")
PUSH2_HIS = [
    "https://push2his.eastmoney.com/api/qt/clist/get",
    "http://push2.eastmoney.com/api/qt/clist/get",
    "https://hsmarketwg.eastmoney.com/HQApi/api/qt/clist/get",
    "https://hsmarketwg.eastmoney.com/HQApi/api/hsindex/real",
]
for u in PUSH2_HIS:
    qs = urllib.parse.urlencode({
        "pn": 1, "pz": 5, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f62", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f62,f184,f66,f72", "_": int(time.time()*1000),
    })
    st, b = get(f"{u}?{qs}", timeout=15)
    txt = b[:300].decode("utf-8", "ignore") if isinstance(b, bytes) else str(b)
    print(f"\n[{u[:50]}] -> HTTP {st}")
    if st == 200 and '"rc":0' in txt or '"data"' in txt:
        # 抽 fields
        try:
            d = json.loads(b)
            diff = d.get("data", {}).get("diff") or d.get("data", [])
            print(f"   响应: {txt[:200]}")
            if isinstance(diff, list) and diff:
                print(f"   首行: {diff[0]}")
            elif isinstance(diff, dict):
                print(f"   diff 是对象（键数 {len(diff)}）")
        except Exception:
            print(f"   raw: {txt[:200]}")
    else:
        print(f"   {txt[:200]}")

# ========== 候选3：hsmarket 主力资金流接口（第三方常见）============
print("\n" + "=" * 60)
print("候选3：hsmarket 资金流接口")
HSM = [
    "https://hsmarketwg.eastmoney.com/HQApi/api/qt/stock/fflow/daykline/get",
    "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get",
    "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
]
for u in HSM:
    qs = urllib.parse.urlencode({
        "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14",
        "lmt": 3, "klt": 101, "secid": "1.600519", "_": int(time.time()*1000),
    })
    st, b = get(f"{u}?{qs}", timeout=15)
    txt = b[:300].decode("utf-8", "ignore") if isinstance(b, bytes) else str(b)
    print(f"\n[{u[:60]}] -> HTTP {st}")
    if st == 200:
        try:
            d = json.loads(b)
            klines = d.get("data", {}).get("klines") or []
            print(f"   rc={d.get('rc')} klines={len(klines)} 首条: {klines[0] if klines else None}")
        except Exception:
            print(f"   raw: {txt[:200]}")
    else:
        print(f"   {txt[:200]}")