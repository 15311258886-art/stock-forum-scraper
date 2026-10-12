# -*- coding: utf-8 -*-
"""取时间渠道复盘：先核 schema，再看两站可恢复性"""
import sqlite3, os, re, json

B = os.getcwd()
con = sqlite3.connect('file:viewer/app.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print("=" * 78)
print("A) raw_pages schema")
print("=" * 78)
cols = [r[1] for r in con.execute("PRAGMA table_info(raw_pages)")]
print("  cols:", cols)

print()
print("=" * 78)
print("B) raw_pages 按站点")
print("=" * 78)
q = "SELECT site, COUNT(*) n, MIN(fetched_at) mn, MAX(fetched_at) mx FROM raw_pages GROUP BY site"
for r in con.execute(q):
    print("  %-12s n=%-5s %s ~ %s" % (r["site"], r["n"], r["mn"], r["mx"]))

print()
print("=" * 78)
print("C) trades 时间填充率（按站点）")
print("=" * 78)
q = """SELECT site, COUNT(*) n,
       SUM(CASE WHEN post_time IS NULL OR TRIM(post_time)='' THEN 1 ELSE 0 END) blank,
       SUM(CASE WHEN post_url LIKE 'http%' THEN 1 ELSE 0 END) hasurl
       FROM trades GROUP BY site"""
for r in con.execute(q):
    print("  %-12s n=%-5s 无时间=%-5s 有URL=%s" % (r["site"], r["n"], r["blank"], r["hasurl"]))

print()
print("=" * 78)
print("D) news 时间填充率（按站点）")
print("=" * 78)
q = """SELECT site, COUNT(*) n,
       SUM(CASE WHEN publish_time IS NULL OR TRIM(publish_time)='' THEN 1 ELSE 0 END) blank
       FROM news GROUP BY site"""
for r in con.execute(q):
    print("  %-12s n=%-5s 无时间=%s" % (r["site"], r["n"], r["blank"]))

print()
print("=" * 78)
print("E) 东财 trades 有 post_url 的样例（可回抓）")
print("=" * 78)
for r in con.execute("""SELECT author, symbol, symbol_name, action, post_url, post_id
     FROM trades WHERE site='eastmoney' AND post_url LIKE 'http%' LIMIT 6"""):
    print("  %-14s %-7s %-8s %-5s" % (r["author"], r["symbol"], r["symbol_name"], r["action"]))
    print("      ", r["post_url"][:110])

print()
print("=" * 78)
print("F) 雪球 trades 情况（已取到时间的）")
print("=" * 78)
for r in con.execute("""SELECT author, symbol, symbol_name, action, post_time, post_url
     FROM trades WHERE site='xueqiu' ORDER BY id DESC LIMIT 10"""):
    print("  %-14s %-7s %-8s %-5s t=%s" % (r["author"] or "-", r["symbol"] or "-",
          (r["symbol_name"] or "-")[:8], r["action"] or "-", r["post_time"] or "<空>"))
    print("      ", (r["post_url"] or "")[:105])

print()
print("=" * 78)
print("G) 雪球 raw_pages 页面清单")
print("=" * 78)
for r in con.execute("""SELECT url, length(html) l FROM raw_pages
     WHERE site='xueqiu' ORDER BY length(html) DESC LIMIT 12"""):
    print("  %8s  %s" % (r["l"], r["url"][:100]))

con.close()
