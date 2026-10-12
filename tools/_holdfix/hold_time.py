# -*- coding: utf-8 -*-
import sqlite3

conn = sqlite3.connect("viewer/app.db")
conn.row_factory = sqlite3.Row
L = []


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    L.append(s)


def q(sql, a=()):
    return conn.execute(sql, a).fetchall()


out("== HOLD trades: time-field coverage")
for r in q("""SELECT COUNT(*) n,
              SUM(CASE WHEN COALESCE(post_time,'')<>'' THEN 1 ELSE 0 END) has_post,
              SUM(CASE WHEN COALESCE(buy_time,'')<>'' THEN 1 ELSE 0 END) has_buy,
              SUM(CASE WHEN COALESCE(sell_time,'')<>'' THEN 1 ELSE 0 END) has_sell
              FROM trades WHERE UPPER(action)='HOLD'"""):
    out("   ", dict(r))
out("   sample HOLD rows:")
for r in q("""SELECT author,symbol,symbol_name,post_time,buy_time,sell_time,price,current_price,quantity
              FROM trades WHERE UPPER(action)='HOLD' LIMIT 6"""):
    out("   ", dict(r))

out("")
out("== HOLD by symbol on post_time window 2026-10-05..2026-10-09 (distinct author)")
for r in q("""SELECT symbol,symbol_name,COUNT(DISTINCT author) n FROM trades
              WHERE UPPER(action)='HOLD'
                AND substr(COALESCE(post_time,''),1,10) BETWEEN '2026-10-05' AND '2026-10-09'
              GROUP BY symbol ORDER BY n DESC LIMIT 10"""):
    out("   ", dict(r))

out("")
out("== HOLD by symbol on buy_time window (distinct author)")
for r in q("""SELECT symbol,symbol_name,COUNT(DISTINCT author) n FROM trades
              WHERE UPPER(action)='HOLD'
                AND substr(COALESCE(buy_time,''),1,10) BETWEEN '2026-10-05' AND '2026-10-09'
              GROUP BY symbol ORDER BY n DESC LIMIT 10"""):
    out("   ", dict(r))

with open("tools/_holdfix/hold_time.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(L))
