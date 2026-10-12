# -*- coding: utf-8 -*-
"""统一核查：雪球抓取入口 + 全站链路状态"""
import os, re, sqlite3, json

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
out = []
W = out.append

# ---------- 1) ingest.py 雪球抓取入口 ----------
p = os.path.join(B, 'viewer', 'ingest.py')
src = open(p, encoding='utf-8').read()
ls = src.splitlines()
W('=== ingest.py TOTAL %d ===' % len(ls))
for i, l in enumerate(ls, 1):
    if re.search(r'xueqiu|browser_get|player_page|search|def ingest|urls\s*=|tpl', l, re.I):
        W('%5d: %s' % (i, l.rstrip()[:170]))
W('')

# ---------- 2) sites.yaml 雪球配置 ----------
y = os.path.join(B, 'config', 'sites.yaml')
if os.path.exists(y):
    W('=== sites.yaml 中 xueqiu 段 ===')
    t = open(y, encoding='utf-8').read()
    m = re.search(r'^xueqiu:.*?(?=^\w|\Z)', t, re.S | re.M)
    W(m.group(0)[:1200] if m else 'NOT FOUND')
    W('')

# ---------- 3) 各站数据量 + 最新时间 ----------
con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row
W('=== 各表数据量 ===')
for t in ['players', 'positions', 'trades', 'posts', 'news', 'raw_pages',
          'lhb_stocks', 'lhb_seats', 'lhb_depts', 'lhb_inst',
          'stock_holders', 'stock_holder_num', 'lhb_sector_focus']:
    try:
        n = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        W('  %-22s %s' % (t, n))
    except Exception as e:
        W('  %-22s ERR %s' % (t, str(e)[:44]))
W('')

W('=== positions 按站点 ===')
for r in con.execute("SELECT site, COUNT(*) n FROM positions GROUP BY site ORDER BY n DESC"):
    W('  %-12s %s' % (r['site'], r['n']))
W('')

W('=== trades 按站点 ===')
for r in con.execute("SELECT site, COUNT(*) n FROM trades GROUP BY site ORDER BY n DESC"):
    W('  %-12s %s' % (r['site'], r['n']))
W('')

W('=== news 按站点 ===')
for r in con.execute("SELECT site, COUNT(*) n FROM news GROUP BY site ORDER BY n DESC"):
    W('  %-12s %s' % (r['site'], r['n']))
W('')

W('=== refresh_status（工程自带源状态）===')
try:
    for r in con.execute("SELECT source_key, display_name, last_ok_at, status, item_count FROM refresh_status ORDER BY source_key"):
        W('  %-16s %-14s %s %-8s n=%s' % (r['source_key'], r['display_name'], r['last_ok_at'], r['status'], r['item_count']))
except Exception as e:
    W('  ERR ' + str(e)[:120])

con.close()
open(os.path.join(B, '_p1_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('written', len(out), 'lines')
