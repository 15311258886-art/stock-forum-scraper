# -*- coding: utf-8 -*-
"""重复打 /api/moneyflow_rank 与 /api/moneyflow_stock，观察盘中/日终口径切换。"""
import json
import time
import urllib.request

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = "http://127.0.0.1:8765"


def get(path):
    with OP.open(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "rank": [], "stock": [], "freshness": []}
for i in range(6):
    m = get("/api/moneyflow_rank?pz=20&force=1")
    out["rank"].append({k: m.get(k) for k in ("ok", "source", "source_label", "data_date",
                                              "data_time", "is_intraday", "trade_date")})
    time.sleep(1)
for i in range(3):
    s = get("/api/moneyflow_stock?symbol=600519")
    out["stock"].append({k: s.get(k) for k in ("ok", "source", "data_date", "data_time",
                                               "is_intraday", "date", "main_net")})
    time.sleep(1)
for i in range(3):
    g = get("/api/market_freshness")
    out["freshness"].append(g.get("market"))
    time.sleep(2)

with open("viewer/logs/_mf_repeat.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=2)
