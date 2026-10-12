# -*- coding: utf-8 -*-
"""对比东财 push2 stock/get：带/不带 `_=<ms>` 缓存清除参数的结果差异。"""
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIELDS = ("f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f86,f92,f105,f116,f117,"
          "f162,f163,f164,f167,f168,f169,f170,f171,f173,f183,f189")
UT = "fa5fd1943c7b386f172d6893dbfba10b"
out = {}
try:
    import emapi

    def probe(with_ts, n=3):
        res = []
        for _ in range(n):
            url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519"
                   f"&fields={FIELDS}&ut={UT}&invt=2&fltt=2")
            if with_ts:
                url += f"&_={int(time.time()*1000)}"
            t0 = time.time()
            j = emapi.get_json(url, "https://quote.eastmoney.com/", timeout=4, retries=0)
            d = (j or {}).get("data")
            res.append({"secs": round(time.time() - t0, 3), "has_data": bool(d),
                        "f92": (d or {}).get("f92"), "f43": (d or {}).get("f43")})
            time.sleep(0.3)
        return res

    out["without_ts"] = probe(False)
    out["with_ts"] = probe(True)
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

with open("viewer/logs/_em_ts.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
