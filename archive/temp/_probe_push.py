"""精探：用正确参数探 push2 资金流接口。"""
from __future__ import annotations
import urllib.request, urllib.parse, json, time

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UT = "fa5fd1943c7b386f172d6893dbfba10b"
REF = "https://quote.eastmoney.com/"

def get(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REF})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(200_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode("utf-8", "ignore")
    except Exception as e:
        return None, repr(e)

# ========== 1. fflow kline —— 多组 klt 试 ==========
print("=" * 60)
print("1. push2 fflow kline 端点（多 klt 试）")
for klt in (1, 5, 15, 30, 60, 101, 102):
    qs = urllib.parse.urlencode({
        "secid": "1.600519",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "klt": klt, "lmt": 5, "ut": UT, "_": int(time.time() * 1000),
    })
    st, txt = get(f"https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?{qs}")
    print(f"\n  klt={klt}: HTTP {st}  len={len(txt)}")
    if st == 200:
        try:
            d = json.loads(txt)
            print(f"    rc={d.get('rc')} data.type={type(d.get('data')).__name__}")
            if d.get("data"):
                ks = (d.get("data") or {}).get("klines") or []
                print(f"    klines count={len(ks)} 首条={ks[0] if ks else None}")
            else:
                print(f"    raw: {txt[:200]}")
        except Exception:
            print(f"    raw: {txt[:200]}")

# ========== 2. clist 主力净流入排行（看 rc 与原因）============
print("\n" + "=" * 60)
print("2. push2 clist 主力净流入排行（多字段组）")
configs = [
    ("m:0+t:6+m:0+t:80+m:1+t:2+m:1+t:23", "f12,f14,f3,f62,f184,f72",
     {"fid": "f62", "po": "1"}),
    # 尝试不带 invt/fltt
    ("m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", "f12,f14,f62",
     {"fid": "f62", "po": "1"}),
    # 全部沪深京
    ("m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048", "f12,f14,f62,f184,f66,f72",
     {"fid": "f62", "po": "1"}),
]
for fs, fields, sort in configs:
    qs = urllib.parse.urlencode({
        "pn": 1, "pz": 5, "po": sort["po"], "np": 1,
        "fid": sort["fid"], "fs": fs, "fields": fields,
        "ut": UT, "invt": 2, "fltt": 2, "_": int(time.time() * 1000),
    })
    st, txt = get(f"https://push2.eastmoney.com/api/qt/clist/get?{qs}")
    print(f"\n  fs={fs[:40]}... fields={fields} sort={sort}")
    print(f"  HTTP {st}  len={len(txt)}")
    if st == 200:
        try:
            d = json.loads(txt)
            rc = d.get("rc"); msg = d.get("message") or d.get("errMsg")
            data = d.get("data") or {}
            diff = data.get("diff") or []
            print(f"    rc={rc} msg={msg!r} diff type={type(diff).__name__} n={len(diff) if isinstance(diff,(list,dict)) else '?'}")
            if isinstance(diff, list) and diff:
                print(f"    首行: {diff[0]}")
            elif isinstance(diff, dict) and diff:
                first_key = sorted(diff.keys(), key=lambda x: int(x) if str(x).isdigit() else x)[0]
                print(f"    首行[{first_key}]: {diff[first_key]}")
        except Exception as e:
            print(f"    parse err: {e}\n    raw: {txt[:300]}")
    else:
        print(f"    raw: {txt[:200]}")