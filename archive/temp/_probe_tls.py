# -*- coding: utf-8 -*-
"""临时探针2：定位 push2 Python 侧失败的真实异常（TLS 遗留重协商？）（用完删）。"""
import json, ssl, sys, time, urllib.request, urllib.parse

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
UT = "fa5fd1943c7b386f172d6893dbfba10b"
PATH = ("/api/qt/clist/get?fid=f62&po=1&pz=3&pn=1&np=1&fltt=2&invt=2"
        "&fs=" + urllib.parse.quote("m:90+t:2+f:!50") + "&fields=f12,f14,f3,f62"
        "&ut=" + UT + "&_=" + str(int(time.time() * 1000)))
HDRS = {"User-Agent": UA, "Referer": "https://data.eastmoney.com/zjlx/list.html"}

print("python ssl:", ssl.OPENSSL_VERSION)

def try_get(label, url, ctx=None, timeout=10):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if ctx is not None:
        op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                         urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(url, headers=HDRS)
        with op.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        j = json.loads(body)
        d = (j.get("data") or {})
        diff = d.get("diff") if isinstance(d, dict) else None
        if isinstance(diff, dict):
            diff = [diff[k] for k in sorted(diff.keys(), key=lambda x: int(x) if str(x).isdigit() else x)]
        print(f"  [{label}] OK rc={j.get('rc')} 条数={len(diff or [])} 首条={json.dumps((diff or [{}])[0], ensure_ascii=False)[:150]}")
        return True
    except Exception as e:
        print(f"  [{label}] FAIL {type(e).__name__}: {str(e)[:180]}")
        return False

hosts = ["push2.eastmoney.com", "82.push2.eastmoney.com", "push2delay.eastmoney.com"]
print("\n=== 1) https 默认上下文（Python 默认，禁用遗留重协商）===")
for h in hosts:
    try_get(f"https://{h}", f"https://{h}{PATH}")

print("\n=== 2) https + 打开 OP_LEGACY_SERVER_CONNECT（允许不安全遗留重协商）===")
ctx2 = ssl.create_default_context()
if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
    ctx2.options |= ssl.OP_LEGACY_SERVER_CONNECT
    print("  (OP_LEGACY_SERVER_CONNECT 已置位)")
else:
    print("  (本 Python 无 OP_LEGACY_SERVER_CONNECT)")
for h in hosts:
    try_get(f"legacy+https://{h}", f"https://{h}{PATH}", ctx=ctx2)

print("\n=== 3) 降级 TLS1.2 + 放开加密套件 ===")
ctx3 = ssl.create_default_context()
try:
    ctx3.maximum_version = ssl.TLSVersion.TLSv1_2
except Exception:
    pass
ctx3.set_ciphers("DEFAULT:@SECLEVEL=1")
if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
    ctx3.options |= ssl.OP_LEGACY_SERVER_CONNECT
for h in hosts:
    try_get(f"tls12+https://{h}", f"https://{h}{PATH}", ctx=ctx3)

print("\n=== 4) 明文 http（绕开 TLS）===")
for h in hosts:
    try_get(f"http://{h}", f"http://{h}{PATH}")
