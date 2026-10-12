import sqlite3, os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=== 1) players 打标全景（site x axis x stale）===')
for r in con.execute('SELECT site, axis, stale, COUNT(*) n FROM players GROUP BY site, axis, stale ORDER BY site'):
    print('  %-11s axis=%-8s stale=%s  n=%s' % (r['site'], r['axis'], r['stale'], r['n']))

print()
print('=== 2) positions 打标全景 ===')
for r in con.execute('SELECT site, axis, stale, COUNT(*) n FROM positions GROUP BY site, axis, stale ORDER BY site'):
    print('  %-11s axis=%-8s stale=%s  n=%s' % (r['site'], r['axis'], r['stale'], r['n']))

print()
print('=== 3) trades 打标全景 ===')
for r in con.execute('SELECT site, axis, stale, COUNT(*) n FROM trades GROUP BY site, axis, stale ORDER BY site'):
    print('  %-11s axis=%-8s stale=%s  n=%s' % (r['site'], r['axis'], r['stale'], r['n']))

print()
print('=== 4) posts 打标全景 ===')
for r in con.execute('SELECT site, axis, stale, COUNT(*) n FROM posts GROUP BY site, axis, stale ORDER BY site'):
    print('  %-11s axis=%-8s stale=%s  n=%s' % (r['site'], r['axis'], r['stale'], r['n']))

print()
print('=== 5) 前端/后端是否已按 stale 过滤 ===')
s = open(os.path.join(B, 'viewer', 'server.py'), encoding='utf-8').read()
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
print('  server.py 含 stale:', s.count('stale'))
print('  app.js   含 stale:', js.count('stale'))
for i, l in enumerate(s.splitlines(), 1):
    if 'stale' in l:
        print('    srv %5d: %s' % (i, l.strip()[:140]))
for i, l in enumerate(js.splitlines(), 1):
    if 'stale' in l:
        print('    js  %5d: %s' % (i, l.strip()[:140]))

con.close()
