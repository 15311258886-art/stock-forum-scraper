import sys, os, re, sqlite3, json
sys.stdout.reconfigure(encoding='utf-8')
B = os.getcwd()
s = open('viewer/db.py', encoding='utf-8').read()
i = s.find('def cohold_recent')
j = s.find('\n    def ', i + 10)
print('===== cohold_recent (span %d-%d) =====' % (i, j))
print(s[i:j][:5000])
print()

con = sqlite3.connect('file:viewer/app.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row
print('=== trades 里 SELL 记录 ===')
for r in con.execute("""SELECT id, site, author, symbol, symbol_name, action, post_time, post_url
    FROM trades WHERE UPPER(COALESCE(action,''))='SELL' ORDER BY post_time DESC"""):
    print('  id=%-4s %-9s %-10s %-7s %-8s %s' % (r['id'], r['site'], (r['author'] or '')[:10], r['symbol'] or '', (r['symbol_name'] or '')[:8], r['post_time']))
print()
print('=== trades action 取值分布 ===')
for r in con.execute("SELECT action, COUNT(*) n FROM trades GROUP BY action ORDER BY n DESC"):
    print('  %r  %s' % (r['action'], r['n']))
print()
print('=== positions 里 action 取值分布 ===')
for r in con.execute("SELECT action, COUNT(*) n FROM positions GROUP BY action ORDER BY n DESC"):
    print('  %r  %s' % (r['action'], r['n']))
print()
print('=== trades 全部时间范围 ===')
r = con.execute("SELECT MIN(post_time) mn, MAX(post_time) mx, COUNT(*) n FROM trades WHERE post_time<>''").fetchone()
print('  ', dict(r))
con.close()
