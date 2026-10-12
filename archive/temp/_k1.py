# -*- coding: utf-8 -*-
"""核实：产业链数据是否落库 / 看板接口是否已接 / 前端是否已渲染"""
import os, sqlite3, re, json

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
out = []
def P(*a):
    s = ' '.join(str(x) for x in a)
    out.append(s)
    print(s)

DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

P('=== 1) sector_chain 相关表 ===')
tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%chain%' OR name LIKE '%sector%' OR name LIKE '%mainline%')")]
P('  tables:', tabs)
for t in tabs:
    n = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
    cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % t)]
    P('  %-24s n=%-6s cols=%s' % (t, n, cols))

P('')
P('=== 2) 产业链表数据样例 ===')
for t in tabs:
    if t == 'lhb_sector_focus':
        continue
    try:
        rows = con.execute('SELECT * FROM "%s" LIMIT 6' % t).fetchall()
        for r in rows:
            d = dict(r)
            P('   ', json.dumps({k: (str(v)[:60] if v is not None else None) for k, v in d.items()}, ensure_ascii=False)[:400])
    except Exception as e:
        P('   ERR', t, e)

P('')
P('=== 3) api_dashboard 是否含 chain ===')
s = open(os.path.join(B, 'viewer', 'server.py'), encoding='utf-8').read()
P('  api_dashboard def:', s.count('def api_dashboard'))
i = s.find('def api_dashboard')
j = s.find('\ndef ', i + 10)
seg = s[i:j]
P('  dashboard 段长度:', len(seg))
for k in ['chain', 'mainline', 'sector_chain', 'leader', '主线', '龙头', '梯队']:
    P('    %-14s %d' % (k, seg.count(k)))
open(os.path.join(B, '_dsrc3.txt'), 'w', encoding='utf-8').write(seg)

P('')
P('=== 4) 前端 renderDashboard 是否含主线 ===')
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
for k in ['renderDashboard', 'chain', 'mainline', '主线', '龙头', '梯队', 'recommend', '推荐']:
    P('    %-18s %d' % (k, js.count(k)))
k = js.find('function renderDashboard')
P('  renderDashboard at', k)
open(os.path.join(B, '_rdash.txt'), 'w', encoding='utf-8').write(js[k:k+6000] if k >= 0 else '')

con.close()
