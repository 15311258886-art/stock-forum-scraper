# -*- coding: utf-8 -*-
"""审计：各站点人数 + 帖子时间滞后分布 + 抓取轴口径"""
import sqlite3, os, datetime, json

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=' * 70)
print('【1】各站点人数 / 有人主页 / 持仓数')
print('=' * 70)
q = """
SELECT p.site,
       COUNT(*) n,
       SUM(CASE WHEN p.source_url IS NOT NULL AND TRIM(p.source_url)<>'' THEN 1 ELSE 0 END) has_url,
       (SELECT COUNT(*) FROM positions x WHERE x.site=p.site) pos_n
FROM players p GROUP BY p.site ORDER BY n DESC
"""
for r in con.execute(q):
    print('  %-10s 人数=%-4s 有主页=%-4s 持仓=%-5s' % (r['site'], r['n'], r['has_url'], r['pos_n']))

print()
print('=' * 70)
print('【2】时间滞后分布（以 2026-09-25 为基准日）')
print('=' * 70)
BASE = datetime.date(2026, 9, 25)
for tbl, col in [('positions', 'post_time'), ('trades', 'post_time'), ('posts', 'post_time'), ('news', 'publish_time')]:
    print('--- %s.%s ---' % (tbl, col))
    rows = con.execute("SELECT site, %s v FROM %s WHERE %s IS NOT NULL AND TRIM(%s)<>''" % (col, tbl, col, col)).fetchall()
    buckets = {}
    for r in rows:
        v = str(r['v'])[:10]
        try:
            d = datetime.date.fromisoformat(v)
        except Exception:
            continue
        lag = (BASE - d).days
        b = '0-1天' if lag <= 1 else ('2-5天' if lag <= 5 else ('6-10天' if lag <= 10 else ('11-30天' if lag <= 30 else '>30天')))
        buckets.setdefault(r['site'], {}).setdefault(b, 0)
        buckets[r['site']][b] += 1
    for site in sorted(buckets):
        print('   %-10s %s' % (site, {k: buckets[site][k] for k in sorted(buckets[site])}))

print()
print('=' * 70)
print('【3】positions 时间字段填充率（决定 chip 能否显示建仓日）')
print('=' * 70)
cols = [r[1] for r in con.execute('PRAGMA table_info(positions)')]
for r in con.execute('SELECT site, COUNT(*) n FROM positions GROUP BY site'):
    s = r['site']
    parts = []
    for c in ['post_time', 'hold_since', 'buy_time']:
        if c in cols:
            v = con.execute("SELECT COUNT(*) FROM positions WHERE site=? AND %s IS NOT NULL AND TRIM(CAST(%s AS TEXT))<>''" % (c, c), (s,)).fetchone()[0]
            parts.append('%s=%d' % (c, v))
    print('  %-10s n=%-4s %s' % (s, r['n'], ' '.join(parts)))

con.close()
