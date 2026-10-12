# -*- coding: utf-8 -*-
"""二分字段表：找出东财 push2 stock/get 稳定可用且含 f92(bps) 的字段子集。"""
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

UTC = "fa5fd1943c7b386f172d6893dbfba10b"
SHORT = "f43,f44,f45,f46,f57,f58,f60,f162,f167,f168,f169,f170"
VARIANTS = {
    "short": SHORT,
    "short+92": SHORT + ",f92",
    "short+47,48": SHORT + ",f47,f48",
    "short+163,164": SHORT + ",f163,f164",
    "short+116,117": SHORT + ",f116,f117",
    "short+86": SHORT + ",f86",
    "short+189": SHORT + ",f189",
    "short+50,171": SHORT + ",f50,f171",
    "short+92+86+189": SHORT + ",f92,f86,f189",
    "full_long": ("f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f86,f92,f105,f116,f117,"
                  "f162,f163,f164,f167,f168,f169,f170,f171,f173,f183,f189"),
}
out = {}
try:
    import emapi

    def one(fields):
        url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519"
               f"&fields={fields}&ut={UTC}&invt=2&fltt=2&_={int(time.time()*1000)}")
        j = emapi.get_json(url, "https://quote.eastmoney.com/", timeout=4, retries=0)
        d = (j or {}).get("data")
        return bool(d), (d or {}).get("f92")

    for name, fields in VARIANTS.items():
        ok, n, bps = 0, 4, None
        for _ in range(n):
            try:
                has, b = one(fields)
                ok += 1 if has else 0
                bps = b if has else bps
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        out[name] = {"ok": f"{ok}/{n}", "bps": bps}
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

with open("viewer/logs/_em_bisect.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
