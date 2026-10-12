# -*- coding: utf-8 -*-
import os, re, sys, sqlite3

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
OUT = os.path.join(B, '_v7_out.txt')
buf = []
def P(*a):
    buf.append(' '.join(str(x) for x in a))

srv = open(os.path.join(B, 'viewer', 'server.py'), encoding='utf-8').read()
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
html = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()

P('================= 后端 =================')
for k in ['def api_star_moves', 'route == "/api/star_moves"', 'def api_niusan', 'route == "/api/niusan"',
          'def api_dashboard', 'route == "/api/dashboard"', 'def api_lhb_sectors', 'route == "/api/lhb_sectors"']:
    P('  %-36s %d' % (k, srv.count(k)))

P('')
P('================= 前端 =================')
for k in ['star_moves', 'renderStarMoves', 'loadStarMoves', 'p-star_moves', 'STARMOVES',
          'niusan', 'renderNiusan', 'p-niusan',
          'dashboard', 'renderDashboard', 'p-dashboard']:
    P('  %-24s app.js=%d' % (k, js.count(k)))

ids = re.findall(r'id="(p-[a-z_]+)"', html)
P('  index panels=%d  p-niusan=%s  p-dashboard=%s  p-star_moves=%s' % (
    len(ids), 'p-niusan' in ids, 'p-dashboard' in ids, 'p-star_moves' in ids))

P('')
P('================= NAV 定义 =================')
i = js.find('const NAV')
j = js.find('\n];', i)
P(js[i:j + 3] if i > 0 else 'NOT FOUND')

P('')
P('================= 数据库关键表 =================')
con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row
for t in ['players', 'positions', 'trades', 'posts', 'news', 'lhb_stocks', 'lhb_seats', 'lhb_depts',
          'lhb_inst', 'stock_holders', 'stock_holder_num', 'lhb_sector_focus']:
    try:
        n = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        P('  %-20s %s' % (t, n))
    except Exception as e:
        P('  %-20s ERR %s' % (t, str(e)[:50]))
con.close()

P('')
P('================= 根目录临时文件 =================')
tmp = sorted([f for f in os.listdir(B) if f.startswith('_') and os.path.isfile(os.path.join(B, f))])
P('  _ 前缀文件 %d 个' % len(tmp))
for f in tmp[:80]:
    P('    %-34s %s' % (f, os.path.getsize(os.path.join(B, f))))

open(OUT, 'w', encoding='utf-8').write('\n'.join(buf))
print('\n'.join(buf))
