"""用正确的 ut + 正确的 fs（带 +f:!2 排除停牌）测试 push2 资金流排行。"""
from __future__ import annotations
import urllib.request, urllib.parse, json, time

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REF = "https://quote.eastmoney.com/"

# 多个 ut 候选（东财接口绑定 ut）
UTs = {
    "A_old": "fa5fd1943c7b386f172d6893dbfba10b",
    "B_akshare": "b2884a393a59ad64002292a3e90d46a5",
    "C_hermes": "b2884a393a59ad64002292a3e90d46a5",
    "D_bre": "bd1ddeb8c5f96d5a7f9b8f05d6e8bba9",
}

# 正确 fs：排除停牌
FS_A = "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2"
FS_B = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"  # 不带排除停牌（旧 fs）

# 字段
FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124"


def get(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REF})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(200_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode("utf-8", "ignore")
    except Exception as e:
        return None, repr(e)


print("=" * 70)
print("Test 1: ut=b2884a3... + fs=带+f:!2 + pz=30")
qs = urllib.parse.urlencode({
    "fid": "f62", "po": 1, "pn": 1, "pz": 30, "np": 1,
    "fltt": 2, "invt": 2, "ut": UTs["B_akshare"],
    "fs": FS_A, "fields": FIELDS,
    "_": int(time.time() * 1000),
})
st, txt = get(f"https://push2.eastmoney.com/api/qt/clist/get?{qs}")
print(f"  HTTP {st}  len={len(txt)}")
print(f"  raw 前 300: {txt[:300]}")
if st == 200 and '"rc"' in txt:
    try:
        d = json.loads(txt)
        print(f"  rc={d.get('rc')} msg={d.get('message')}")
        diff = (d.get('data') or {}).get('diff') or []
        print(f"  diff 类型={type(diff).__name__} n={len(diff) if isinstance(diff, (list, dict)) else 0}")
        if isinstance(diff, list) and diff:
            print(f"  首行: {diff[0]}")
        elif isinstance(diff, dict):
            k0 = sorted(diff.keys(), key=lambda x: int(x) if str(x).isdigit() else x)[0]
            print(f"  首行[{k0}]: {diff[k0]}")
    except Exception as e:
        print(f"  parse err: {e}")

# 加 Referer: https://data.eastmoney.com/zjlx/list.html （从数据中心页）
print("\n" + "=" * 70)
print("Test 2: 加 referer=数据中心页 + ut=b2884a3 + fs=带 +f:!2")
qs = urllib.parse.urlencode({
    "fid": "f62", "po": 1, "pn": 1, "pz": 30, "np": 1,
    "fltt": 2, "invt": 2, "ut": UTs["B_akshare"],
    "fs": FS_A, "fields": FIELDS,
    "_": int(time.time() * 1000),
})
url = f"https://push2.eastmoney.com/api/qt/clist/get?{qs}"
try:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/zjlx/list.html"})
    with urllib.request.urlopen(req, timeout=20) as r:
        txt = r.read(200_000).decode("utf-8", "ignore")
        st = r.status
        print(f"  HTTP {st}  len={len(txt)}")
        print(f"  raw 前 300: {txt[:300]}")
        if '"rc"' in txt:
            d = json.loads(txt)
            print(f"  rc={d.get('rc')} msg={d.get('message')}")
            diff = (d.get('data') or {}).get('diff') or []
            if isinstance(diff, list) and diff:
                print(f"  diff n={len(diff)}, 首行: {diff[0]}")
except Exception as e:
    print(f"  ERR: {e}")

# 个股资金流：fflow kline
print("\n" + "=" * 70)
print("Test 3: fflow kline / 端点 / klt=101")
for path in [
    "/api/qt/stock/fflow/kline/get",
    "/api/qt/stock/fflow/daykline/get",
    "/api/qt/stock/fflow/minute/get",
]:
    qs = urllib.parse.urlencode({
        "secid": "1.600519",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "klt": 101, "lmt": 5, "ut": UTs["B_akshare"], "_": int(time.time() * 1000),
    })
    url = f"https://push2.eastmoney.com{path}?{qs}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REF})
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read(200_000).decode("utf-8", "ignore")
            d = json.loads(txt)
            ks = (d.get('data') or {}).get('klines') or []
            print(f"  {path}: rc={d.get('rc')} klines={len(ks)} 首条={ks[0] if ks else None}")
    except Exception as e:
        print(f"  {path}: ERR {repr(e)[:80]}")

# 板块资金流 m:90+t:2（盘面/行业）
print("\n" + "=" * 70)
print("Test 4: 板块资金流 m:90+t:2 / fid=f62")
qs = urllib.parse.urlencode({
    "fid": "f62", "po": 1, "pn": 1, "pz": 30, "np": 1,
    "fltt": 2, "invt": 2, "ut": UTs["B_akshare"],
    "fs": "m:90+t:2", "fields": "f12,f14,f3,f62,f184,f66,f204",
    "_": int(time.time() * 1000),
})
try:
    req = urllib.request.Request(f"https://push2.eastmoney.com/api/qt/clist/get?{qs}",
                                headers={"User-Agent": UA, "Referer": REF})
    with urllib.request.urlopen(req, timeout=20) as r:
        txt = r.read(200_000).decode("utf-8", "ignore")
        d = json.loads(txt)
        diff = (d.get('data') or {}).get('diff') or []
        print(f"  rc={d.get('rc')} diff n={len(diff) if isinstance(diff,list) else 'obj'}")
        if isinstance(diff, list) and diff:
            print(f"  首行: {diff[0]}")
except Exception as e:
    print(f"  ERR: {repr(e)[:100]}")