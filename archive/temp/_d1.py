# -*- coding: utf-8 -*-
"""核查：持仓可用的日期来源 + chip 渲染代码位置"""
import sqlite3, os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=== 1) positions 各站点 / 各日期字段有值数 ===')
cols = [r[1] for r in con.execute('PRAGMA table_info(positions)')]
print('cols:', cols)
tot = con.execute('SELECT COUNT(*) FROM positions').fetchone()[0]
print('total positions:', tot)
for r in con.execute('SELECT site, COUNT(*) n FROM positions GROUP BY site'):
    s = r['site']
    parts = ['%-10s n=%-4s' % (s, r['n'])]
    for c in ('post_time', 'hold_since', 'buy_time'):
        if c in cols:
            v = con.execute(
                "SELECT COUNT(*) FROM positions WHERE site=? AND %s IS NOT NULL AND TRIM(CAST(%s AS TEXT))<>''" % (c, c),
                (s,)).fetchone()[0]
            parts.append('%s=%s' % (c, v))
    print('  ' + ' | '.join(parts))

print()
print('=== 2) taoguba posts 有无日期（可交叉印证）===')
for r in con.execute("SELECT COUNT(*) n, MIN(post_time) mn, MAX(post_time) mx FROM posts WHERE site='taoguba'"):
    print('  taoguba posts: n=%s  %s ~ %s' % (r['n'], r['mn'], r['mx']))
for r in con.execute("SELECT site, COUNT(*) n, MIN(post_time) mn, MAX(post_time) mx FROM posts GROUP BY site"):
    print('  %-10s n=%-5s %s ~ %s' % (r['site'], r['n'], r['mn'], r['mx']))

print()
print('=== 3) trades 带日期、且作者在 positions 里也持仓的（可交叉补建仓日）===')
q = '''SELECT COUNT(*) FROM positions p WHERE EXISTS (
  SELECT 1 FROM trades t WHERE t.author=p.player AND t.symbol=p.symbol
    AND t.post_time IS NOT NULL AND TRIM(t.post_time)<>'')'''
print('  同选手+同代码可补:', con.execute(q).fetchone()[0])
q2 = '''SELECT COUNT(*) FROM positions p WHERE EXISTS (
  SELECT 1 FROM posts po WHERE po.author=p.player
    AND po.post_time IS NOT NULL AND TRIM(po.post_time)<>'')'''
print('  仅同选手有帖（参考）:', con.execute(q2).fetchone()[0])
con.close()

print()
print('=== 4) app.js 中持仓 chip 渲染代码 ===')
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read().splitlines()
for i, l in enumerate(js, 1):
    if re.search(r'hold-chip|chip-time|hls\.slice|hlShown|关注/操作|未披露', l):
        print('%5d: %s' % (i, l.rstrip()[:190]))
