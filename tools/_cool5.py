# -*- coding: utf-8 -*-
"""冷却后连打 5 次：验证「东财健康时 /api/quote 连打 5 次 valuation.bps 均非 null」+ 耗时。"""
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


def hit():
    t0 = time.time()
    with OP.open("http://127.0.0.1:8765/api/quote?symbol=600519&force=1", timeout=30) as r:
        j = json.loads(r.read().decode("utf-8", "ignore"))
    return {
        "secs": round(time.time() - t0, 3),
        "source": (j.get("source") or "")[:18],
        "valuation_bps": (j.get("valuation") or {}).get("bps"),
        "finance_bps": (j.get("finance") or {}).get("bps"),
        "pe_ttm": (j.get("valuation") or {}).get("pe_ttm"),
        "price": (j.get("quote") or {}).get("price"),
    }


try:
    time.sleep(150)          # 冷却：等东财配额窗口过去（期间不发任何 EM 请求）
    rows = [hit() for _ in range(5)]
    out["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out["rows"] = rows
    out["valuation_bps_all_nonnull"] = all(r["valuation_bps"] is not None for r in rows)
    out["secs"] = [r["secs"] for r in rows]
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

with open(os.path.join(ROOT, "viewer", "logs", "_cool5.json"), "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
print("WROTE")
