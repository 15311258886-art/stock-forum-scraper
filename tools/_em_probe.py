# -*- coding: utf-8 -*-
"""核对东财 push2 单只 quote（stock/get）当前是否可达，以及 bps 字段是否随 EM 返回。"""
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

out = {}
try:
    import emapi

    for secid in ("1.600519", "1.000001", "0.000001"):
        t0 = time.time()
        q = emapi.quote(secid, timeout=4, retries=0)
        out[secid] = {"secs": round(time.time() - t0, 3),
                      "ok": bool(q), "bps_from_api": q.get("pb") if q else None,
                      "keys": sorted(q.keys()) if q else None}
    # 直接用服务同款 URL/字段看 f92（每股净资产）
    url = ("https://push2.eastmoney.com/api/qt/stock/get"
           "?secid=1.600519&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f86,f92,f105,f116,f117,"
           "f162,f163,f164,f167,f168,f169,f170,f171,f173,f183,f189&ut=fa5fd1943c7b386f172d6893dbfba10b"
           "&invt=2&fltt=2")
    t0 = time.time()
    j = emapi.get_json(url, "https://quote.eastmoney.com/", timeout=4, retries=0)
    d = (j or {}).get("data")
    out["raw_stock_get"] = {"secs": round(time.time() - t0, 3), "has_data": bool(d),
                            "f92": (d or {}).get("f92"), "f43": (d or {}).get("f43"),
                            "f58": (d or {}).get("f58")}
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

with open("viewer/logs/_em_probe.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
