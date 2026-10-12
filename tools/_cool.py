# -*- coding: utf-8 -*-
"""冷却后单发：等 EM 风控窗口过去，单次请求验证「EM 健康 -> bps 非空」。"""
import json
import os
import sys
import time
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
out = {}
try:
    import emapi

    time.sleep(150)                      # 冷却，期间不发任何 EM 请求
    t0 = time.time()
    q = emapi.quote("1.600519", timeout=5, retries=0)
    out["emapi_quote"] = {"secs": round(time.time() - t0, 3), "ok": bool(q),
                          "pb_from_emapi": (q or {}).get("pb")}

    t0 = time.time()
    with OP.open("http://127.0.0.1:8765/api/quote?symbol=600519&force=1", timeout=30) as r:
        j = json.loads(r.read().decode("utf-8", "ignore"))
    out["server_api_quote"] = {"secs": round(time.time() - t0, 3),
                               "source": (j.get("source") or "")[:20],
                               "bps": (j.get("valuation") or {}).get("bps"),
                               "pe_ttm": (j.get("valuation") or {}).get("pe_ttm"),
                               "price": (j.get("quote") or {}).get("price")}
    out["at"] = time.strftime("%H:%M:%S")
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

with open("viewer/logs/_cool.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
