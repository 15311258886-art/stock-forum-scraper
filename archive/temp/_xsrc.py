import os, sqlite3, json, re, collections

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=== 1) 东财玩家（players）样例 ===')
for r in con.execute("SELECT name, site, channel, tier, source_url, real_holding FROM players WHERE site='eastmoney' LIMIT 6"):
    print('  ', dict(r))
print('  东财玩家总数:', con.execute("SELECT COUNT(*) FROM players WHERE site='eastmoney'").fetchone()[0])
print()

print('=== 2) 东财 trades/positions 的时间填充 ===')
for t, c in [('trades', 'post_time'), ('positions', 'post_time')]:
    for r in con.execute("SELECT site, COUNT(*) n, SUM(CASE WHEN %s IS NULL OR TRIM(%s)='' THEN 1 ELSE 0 END) blank FROM %s WHERE site='eastmoney' GROUP BY site" % (c, c, t)):
        print('  %s.%s: n=%s blank=%s' % (t, c, r['n'], r['blank']))
print()

print('=== 3) 东财 trades 明细（看有无 url / post_id / raw_text） ===')
for r in con.execute("SELECT * FROM trades WHERE site='eastmoney' LIMIT 6"):
    d = dict(r)
    print('  ', {k: (str(v)[:60] if v is not None else None) for k, v in d.items()})
print()

print('=== 4) raw_pages 里东财存了什么页 ===')
for r in con.execute("SELECT site, COUNT(*) n, MIN(fetched_at) mn, MAX(fetched_at) mx, MAX(length(html)) mxlen FROM raw_pages GROUP BY site"):
    print('  %-12s n=%-5s %s ~ %s  maxlen=%s' % (r['site'], r['n'], r['mn'], r['mx'], r['mxlen']))
print()
print('  --- eastmoney 页面 url ---')
for r in con.execute("SELECT url, length(html) l, fetched_at FROM raw_pages WHERE site='eastmoney' ORDER BY rowid DESC LIMIT 12"):
    print('   ', r['fetched_at'], r['l'], r['url'][:110])
print('  --- xueqiu 页面 url ---')
for r in con.execute("SELECT url, length(html) l, fetched_at FROM raw_pages WHERE site='xueqiu' ORDER BY rowid DESC LIMIT 10"):
    print('   ', r['fetched_at'], r['l'], r['url'][:110])
con.close()
