import json, sqlite3, urllib.request

BASE = "http://127.0.0.1:8765"
DB = r"F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper/viewer/app.db"

print("=== 1) API /api/board_flow_intraday ===")
with urllib.request.urlopen(BASE + "/api/board_flow_intraday?kind=industry", timeout=20) as r:
    d = json.load(r)
print("  ok =", d.get("ok"))
for k in ("data_date", "trade_date", "is_trading_day", "latest_trade_date",
          "stale", "market_note", "source", "source_label", "updated_at"):
    print("   %-18s = %s" % (k, d.get(k)))
s = d.get("series") or []
print("  axis 点数 =", len(d.get("axis") or []), "| series 条数 =", len(s))
for x in s[:4]:
    print("   -", {k: (len(x[k]) if isinstance(x.get(k), list) else x.get(k))
                   for k in x.keys()})

print()
print("=== 2) 库内日期盘点 ===")
c = sqlite3.connect(DB)
cur = c.cursor()
for t in ("board_flow_intraday", "board_flow_samples"):
    print("---", t)
    try:
        cur.execute("select trade_date, count(*) from %s group by trade_date order by trade_date desc limit 6" % t)
        for row in cur.fetchall():
            print("   ", row)
    except Exception as e:
        print("    ERR", e)

print()
print("=== 3) snapshot 日期 ===")
try:
    with urllib.request.urlopen(BASE + "/api/board_flow_snapshot", timeout=20) as r:
        sd = json.load(r)
    print("  keys:", {k: sd.get(k) for k in ("ok", "trade_date", "latest_trade_date", "is_trading_day", "data_date") if k in sd})
except Exception as e:
    print("  snapshot 接口单独探测:", e)
