# -*- coding: utf-8 -*-
"""测试东财风控是否按 ut 计：同一时刻交替用两个 ut 请求长字段表。"""
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LONG = ("f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f86,f92,f105,f116,f117,"
        "f162,f163,f164,f167,f168,f169,f170,f171,f173,f183,f189")
UT_A = "fa5fd1943c7b386f172d6893dbfba10b"
UT_B = "b2884a393a59ad64002292a3e90d46a5"
seq = []
try:
    import emapi

    def call(ut):
        url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519"
               f"&fields={LONG}&ut={ut}&invt=2&fltt=2&_={int(time.time()*1000)}")
        j = emapi.get_json(url, "https://quote.eastmoney.com/", timeout=4, retries=0)
        d = (j or {}).get("data")
        return bool(d), (d or {}).get("f92")

    for i in range(5):
        ha, ba = call(UT_A)
        seq.append({"i": i, "ut": "A", "ok": ha, "f92": ba})
        time.sleep(0.35)
        hb, bb = call(UT_B)
        seq.append({"i": i, "ut": "B", "ok": hb, "f92": bb})
        time.sleep(0.35)
except Exception:  # noqa: BLE001
    seq.append({"fatal": traceback.format_exc()})

with open("viewer/logs/_em_ut.json", "w", encoding="utf-8") as fh:
    json.dump(seq, fh, ensure_ascii=False, indent=2)
