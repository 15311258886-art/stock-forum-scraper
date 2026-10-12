
# -*- coding: utf-8 -*-
"""核查 quality_filter 打标结果 + 持仓日期现状"""
import os, sqlite3

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=== players 轴分布（按站点）===')
try:
    for r in con.execute('SELECT site, axis, COUNT(*) n FROM players GROUP BY site, axis ORDER BY site'):
        print('  %-11s axis=%-10s %s' % (r['site'], r['axis'], r['n']))
except Exception as e:
    print('  ERR', e)

print()
print('=== positions 轴/时效分布 ===')
try:
    for r in con.execute('SELECT site, axis, stale, COUNT(*) n FROM positions GROUP BY site, axis, stale ORDER BY site'):
        print('  %-11s axis=%-10s stale=%s  %s' % (r['site'], r['axis'], r['stale'], r['n']))
except Exception as e:
    print('  ERR', e)

print()
print('=== posts 时效分布 ===')
try:
    for r in con.execute('SELECT site, axis, stale, COUNT(*) n FROM posts GROUP BY site, axis, stale ORDER BY site'):
        print('  %-11s axis=%-10s stale=%s  %s' % (r['site'], r['axis'], r['stale'], r['n']))
except Exception as e:
    print('  ERR', e)

print()
print('=== news 时效分布 ===')
try:
    for r in con.execute('SELECT site, axis, stale, COUNT(*) n FROM news GROUP BY site, axis, stale ORDER BY site'):
        print('  %-11s axis=%-10s stale=%s  %s' % (r['site'], r['axis'], r['stale'], r['n']))
except Exception as e:
    print('  ERR', e)

print()
print('=== positions 日期字段填充 ===')
cols = [r[1] for r in con.execute('PRAGMA table_info(positions)')]
print('  cols:', cols)
for r in con.execute('SELECT site, COUNT(*) n FROM positions GROUP BY site'):
    s = r['site']
    parts = []
    for c in ('post_time', 'hold_since', 'buy_time'):
        if c in cols:
            v = con.execute("SELECT COUNT(*) FROM positions WHERE site=? AND %s IS NOT NULL AND TRIM(CAST(%s AS TEXT))<>''" % (c, c), (s,)).fetchone()[0]
            parts.append('%s=%s' % (c, v))
    print('  %-11s n=%-4s %s' % (s, r['n'], ' | '.join(parts)))

con.close()
