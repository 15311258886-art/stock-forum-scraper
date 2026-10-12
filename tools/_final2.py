# -*- coding: utf-8 -*-
"""P1 收尾终检 v2：冷却后 ×5 + market 层级 + 回归（一次跑齐，结果写 viewer/logs/_final2.json）。"""
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
BASE = "http://127.0.0.1:8765"
out = {}


def getraw(path, timeout=30):
    t0 = time.time()
    try:
        with OP.open(BASE + path, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "ignore"), time.time() - t0
    except Exception as e:  # noqa: BLE001
        return None, f"ERR:{e}", time.time() - t0


def quote_row(i):
    st, body, el = getraw("/api/quote?symbol=600519&force=1")
    try:
        j = json.loads(body)
    except Exception:  # noqa: BLE001
        j = {}
    val, fin, q = j.get("valuation") or {}, j.get("finance") or {}, j.get("quote") or {}
    return {"i": i, "status": st, "secs": round(el, 3),
            "source": (j.get("source") or "")[:16],
            "valuation_bps": val.get("bps"), "finance_bps": fin.get("bps"),
            "pe_ttm": val.get("pe_ttm"), "price": q.get("price")}


try:
    time.sleep(130)   # 冷却（期间不发 EM 请求）
    out["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out["quote_x5"] = [quote_row(i + 1) for i in range(5)]
    out["valuation_bps_all_nonnull"] = all(r["valuation_bps"] is not None for r in out["quote_x5"])
    out["finance_bps_all_nonnull"] = all(r["finance_bps"] is not None for r in out["quote_x5"])

    st, body, el = getraw("/api/market_freshness")
    mf = json.loads(body)
    out["market_freshness"] = {"status": st, "secs": round(el, 3), "body": mf}

    st, body, el = getraw("/api/refresh_status")
    rs = json.loads(body)
    out["refresh_status"] = {"status": st, "has_market_key": "market" in rs,
                             "market": rs.get("market")}

    reg = {}
    for p in ["/", "/api/refresh_status", "/api/stock_names",
              "/api/detail_list?kind=posts&limit=5", "/api/hotspots",
              "/api/scheduler/status", "/api/market_freshness"]:
        st, body, el = getraw(p)
        reg[p] = {"status": st, "secs": round(el, 3)}
    out["regression"] = reg
except Exception:  # noqa: BLE001
    out["fatal"] = traceback.format_exc()

with open(os.path.join(ROOT, "viewer", "logs", "_final2.json"), "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
print("WROTE")
