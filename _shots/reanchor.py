"""2026-10-05：拿库里今天已归档的 30 行，按当前 K 线重算「最值得入手价」再归档一次。
目的不是刷数据，而是让 rate_buy_point 锚点首次落库（anchor_price 得是当时现价，
否则跟踪列少了"锚点日现价"这条对照）。幂等：同日覆盖，锚点只建一次。"""
import sqlite3
import screener

SNAP = "2026-10-05"
c = sqlite3.connect("viewer/app.db")
c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute(
    "SELECT * FROM rate_snap WHERE snap_date=? ORDER BY rank LIMIT 30", (SNAP,))]
c.close()
if not rows:
    raise SystemExit("rate_snap 里没有 %s 的归档，先跑一次榜" % SNAP)

bm, _ = screener._load_bars_map({r["code"]: "" for r in rows}, 30)
out = []
for r in rows:
    bars = bm.get(r["code"]) or []
    ib = screener.ideal_buy_point(bars, r["price"]) if bars else (None, None, None)
    d = dict(r)
    d["ideal_buy"], _g, d["ideal_dist"] = ib
    out.append(d)

res = screener.snapshot_top(out, src="re-anchor", trade_date=rows[0].get("trade_date") or "")
print("归档:", res)

am = screener.rate_buy_map()
print("锚点数:", len(am))
for code in [r["code"] for r in out][:5]:
    print("  ", code, am.get(code))
