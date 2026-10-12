"""2026-10-05 一次性：重跑一次真实归档链路（turnover_top → POST /api/rate/snapshot），
把 trade_date 一并带进去落库，时间轴的基准日才准。慢路，几分钟。"""
import json
import urllib.request
import urllib.error
import sys

sys.path.insert(0, ".")

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本地 POST 会被系统代理拦 502

import screener  # noqa: E402

print("scanning full market ...", flush=True)
d = screener.turnover_top(days=30, limit=30)
if not d.get("ok"):
    print("TURNVER TOP FAIL", d)
    sys.exit(1)
rows = d.get("rows") or []
td = d.get("trade_date") or ""
print("rows=%d trade_date=%r pool=%s" % (len(rows), td, d.get("pool_size")), flush=True)

body = json.dumps({"rows": rows, "src": "manual", "trade_date": td}).encode("utf-8")
req = urllib.request.Request("http://127.0.0.1:8765/api/rate/snapshot", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
try:
    with op.open(req, timeout=900) as r:
        print("SNAPSHOT", r.read().decode("utf-8", "ignore"))
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode("utf-8", "ignore")[:400])
    sys.exit(1)
