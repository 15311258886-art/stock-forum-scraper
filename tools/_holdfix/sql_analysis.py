# -*- coding: utf-8 -*-
"""SQL analysis: (a) is cohold affected by HOLD?  (b) tier-A filter effect."""
import sqlite3

DB = "viewer/app.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
L = []


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    L.append(s)


def q(sql, args=()):
    return conn.execute(sql, args).fetchall()


out("== positions.action vs trades.action (cohold filter is UPPER(action)=BUY/SELL)")
for r in q("SELECT action, COUNT(*) n FROM positions GROUP BY action"):
    out("   positions", dict(r))
for r in q("SELECT action, COUNT(*) n FROM trades GROUP BY action"):
    out("   trades   ", dict(r))

out("")
out("== HOLD rows: do any carry sell_time (would wrongly land in sell group)?")
for r in q("SELECT action, SUM(CASE WHEN COALESCE(sell_time,'')<>'' THEN 1 ELSE 0 END) has_sell,"
           " SUM(CASE WHEN COALESCE(buy_time,'')<>'' THEN 1 ELSE 0 END) has_buy, COUNT(*) n"
           " FROM trades GROUP BY action"):
    out("   ", dict(r))

out("")
out("== window 2026-10-05..2026-10-09: BUY trades by symbol (distinct author)")
for r in q("""SELECT symbol, symbol_name, COUNT(DISTINCT author) n FROM trades
              WHERE UPPER(COALESCE(action,''))='BUY'
                AND substr(COALESCE(buy_time,post_time),1,10) BETWEEN '2026-10-05' AND '2026-10-09'
              GROUP BY symbol ORDER BY n DESC LIMIT 10"""):
    out("   ", dict(r))
out("   -- HOLD trades in same window (should be 0 in cohold buy group):")
for r in q("""SELECT symbol, symbol_name, COUNT(DISTINCT author) n FROM trades
              WHERE UPPER(COALESCE(action,''))='HOLD'
                AND substr(COALESCE(buy_time,post_time),1,10) BETWEEN '2026-10-05' AND '2026-10-09'
              GROUP BY symbol ORDER BY n DESC LIMIT 10"""):
    out("   ", dict(r))

out("")
out("== tier-A share among trades authors")
for r in q("""SELECT t.action, COUNT(DISTINCT t.author) total,
                     COUNT(DISTINCT CASE WHEN pl.tier='A' THEN t.author END) tierA
              FROM trades t LEFT JOIN players pl ON pl.name=t.author
              GROUP BY t.action"""):
    out("   ", dict(r))

out("")
out("== consensus holders recomputed with 3 buckets (top 12), min 2")
rows = q("""SELECT p.symbol, p.symbol_name, p.player, p.action, p.post_time,
                   COALESCE(pl.tier, p.channel, 'D') tier
            FROM positions p LEFT JOIN players pl ON pl.name=p.player
            WHERE p.symbol IS NOT NULL AND p.symbol<>''""")
tr = q("""SELECT t.symbol, t.symbol_name, t.author player, t.action, t.post_time,
                 COALESCE(pl.tier,'D') tier
          FROM trades t LEFT JOIN players pl ON pl.name=t.author
          WHERE t.symbol IS NOT NULL AND t.symbol<>'' AND t.author IS NOT NULL AND t.author<>''
            AND UPPER(COALESCE(t.action,'')) IN ('BUY','HOLD')
            AND NOT EXISTS (SELECT 1 FROM trades t2 WHERE t2.author=t.author AND t2.symbol=t.symbol
              AND (COALESCE(t2.post_time,'')>COALESCE(t.post_time,'')
                   OR (COALESCE(t2.post_time,'')=COALESCE(t.post_time,'') AND t2.id>t.id)))""")


def cls(action):
    a = (action or "").upper()
    if a == "BUY":
        return "BUY"
    if a == "HOLD":
        return "HOLD"
    return "WATCH"


# priority per (symbol, player): BUY > HOLD > WATCH
pr = {"BUY": 3, "HOLD": 2, "WATCH": 1}
best = {}
for r in list(rows) + list(tr):
    k = (r["symbol"], r["player"])
    c = cls(r["action"])
    if k not in best or pr[c] > pr[best[k][0]]:
        best[k] = (c, r["tier"], r["symbol_name"])
agg = {}
for (sym, pl), (c, tier, nm) in best.items():
    d = agg.setdefault(sym, {"name": nm, "b": 0, "h": 0, "w": 0, "ba": 0})
    d[{"BUY": "b", "HOLD": "h", "WATCH": "w"}[c]] += 1
    if c == "BUY" and tier == "A":
        d["ba"] += 1
ranked = sorted(agg.items(), key=lambda kv: (kv[1]["b"] + kv[1]["h"] + kv[1]["w"]), reverse=True)
for sym, d in ranked[:12]:
    out("   %7s %-9s total=%2d buy=%2d hold=%2d watch=%2d  (buy_tierA=%d)" % (
        sym, (d["name"] or "")[:8], d["b"] + d["h"] + d["w"], d["b"], d["h"], d["w"], d["ba"]))

with open("tools/_holdfix/sql_analysis.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(L))
