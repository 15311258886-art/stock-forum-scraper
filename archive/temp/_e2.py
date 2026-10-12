# -*- coding: utf-8 -*-
"""剔除规则设计：非人轴来源 + 滞后>5天 → 剔除（先 dry-run）"""
import sqlite3, os, datetime

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=' * 70)
print('【表结构】')
print('=' * 70)
for t in ['posts', 'news', 'trades', 'positions', 'players']:
    cols = [(r[1], r[2]) for r in con.execute('PRAGMA table_info(%s)' % t)]
    print('  %-10s %s' % (t, ', '.join('%s:%s' % c for c in cols)))
    print()

print('=' * 70)
print('【剔除预案】非人轴来源(eastmoney/ths/jisilu) + 帖子滞后>5天')
print('=' * 70)
BASE = datetime.date(2026, 9, 25)
CUT = (BASE - datetime.timedelta(days=5)).isoformat()   # 2026-09-20
print('  基准日 %s，滞后阈值 5 天 → 保留 >= %s' % (BASE, CUT))
print()

plan = {}
for tbl, col in [('posts', 'post_time'), ('news', 'publish_time'), ('trades', 'post_time')]:
    print('--- %s ---' % tbl)
    for site in ['eastmoney', 'ths', 'jisilu']:
        tot = con.execute("SELECT COUNT(*) FROM %s WHERE site=?" % tbl, (site,)).fetchone()[0]
        if not tot:
            continue
        blank = con.execute("SELECT COUNT(*) FROM %s WHERE site=? AND (%s IS NULL OR TRIM(%s)='')" % (tbl, col, col), (site,)).fetchone()[0]
        keep = con.execute("SELECT COUNT(*) FROM %s WHERE site=? AND substr(%s,1,10)>=?" % (tbl, col), (site, CUT)).fetchone()[0]
        old = tot - blank - keep
        print('  %-10s 总=%-5s 无时间=%-5s 保留(近5天)=%-4s 剔除(>5天)=%s' % (site, tot, blank, keep, old))
        plan[(tbl, site)] = (tot, blank, keep, old)

print()
print('=' * 70)
print('【jisilu 专项】2021 年历史帖 → 建议整源剔除')
print('=' * 70)
for r in con.execute("SELECT site, player, symbol_name, ratio, post_time FROM positions WHERE site='jisilu'"):
    print('  ', dict(r))

con.close()
