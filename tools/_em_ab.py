# -*- coding: utf-8 -*-
"""交替请求 短字段 vs 长字段，隔离「字段表」与「时间/限流」两个变量。"""
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
UTC = "fa5fd1943c7b386f172d6893dbfba10b"
seq = []
try:
    import emapi

    def call(fields):
        url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519"
               f"&fields={fields}&ut={UTC}&invt=2&fltt=2&_={int(time.time()*1000)}")
        j = emapi.get_json(url, "https://quote.eastmoney.com/", timeout=4, retries=0)
        d = (j or {}).get("data")
        return bool(d), (d or {}).get("f92")

    # 先「长-长」起手，排除「首个请求占便宜」的假象
    for i in range(6):
        h, b = call(LONG)
        seq.append({"i": i, "tag": "long", "ok": h, "f92": b})
        time.sleep(0.4)
        h, b = call(SHORT)
        seq.append({"i": i, "tag": "short", "ok": h, "f92": b})
        time.sleep(0.4)
    # 再来一轮：长字段，间隔 2s（宽松节奏），看是否仍全败
    for i in range(4):
        h, b = call(LONG)
        seq.append({"i": i, "tag": "long_slow", "ok": h, "f92": b})
        time.sleep(2.0)
except Exception:  # noqa: BLE001
    seq.append({"fatal": traceback.format_exc()})

with open("viewer/logs/_em_ab.json", "w", encoding="utf-8") as fh:
    json.dump(seq, fh, ensure_ascii=False, indent=2)
