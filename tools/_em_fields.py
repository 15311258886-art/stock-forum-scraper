# -*- coding: utf-8 -*-
"""隔离变量：同一时刻交替请求「短字段表(emapi.quote)」与「长字段表(server _QUOTE_FIELDS)」。"""
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SHORT = "f43,f44,f45,f46,f57,f58,f60,f162,f167,f168,f169,f170"
LONG = ("f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f86,f92,f105,f116,f117,"
        "f162,f163,f164,f167,f168,f169,f170,f171,f173,f183,f189")
UT = "fa5fd1943c7b386f172d6893dbfba10b"
out = []
try:
    import emapi

    def call(fields, tag):
        url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519"
               f"&fields={fields}&ut={UT}&invt=2&fltt=2&_={int(time.time()*1000)}")
        t0 = time.time()
        j = emapi.get_json(url, "https://quote.eastmoney.com/", timeout=4, retries=0)
        d = (j or {}).get("data")
        return {"tag": tag, "secs": round(time.time() - t0, 3), "has_data": bool(d),
                "f92": (d or {}).get("f92"), "f43": (d or {}).get("f43"),
                "rc": (j or {}).get("rc")}

    for i in range(4):
        out.append(call(SHORT, "short"))
        time.sleep(0.2)
        out.append(call(LONG, "long"))
        time.sleep(0.2)
except Exception:  # noqa: BLE001
    out.append({"fatal": traceback.format_exc()})

with open("viewer/logs/_em_fields.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
