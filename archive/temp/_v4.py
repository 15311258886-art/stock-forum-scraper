# -*- coding: utf-8 -*-
"""全面状态核查：前端接线 / 后端路由 / 数据量 / 股东打标现状"""
import os, re, io, sys, sqlite3

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='ignore')
B = r'F:/AI Agent部署知识库/2026-09-22-11-49-42/stock-forum-scraper'
js = open(os.path.join(B, 'viewer/static/app.js'), encoding='utf-8').read()
h = open(os.path.join(B, 'viewer/static/index.html'), encoding='utf-8').read()
s = open(os.path.join(B, 'viewer/server.py'), encoding='utf-8').read()

print('=== 前端接线 ===')
for k in ['renderDashboard', 'loadDashboard', 'tab: "dashboard"', 'DASH', 'p-dashboard',
          'renderNiusan', 'loadNiusan', 'tab: "niusan"', 'p-niusan',
          'renderLhbSectors', 'loadLhbSectors', 'tab: "lhb_sectors"', 'p-lhb_sectors']:
    print('  app.js %-24s %d' % (k, js.count(k)))
ids = re.findall(r'id="(p-[a-z_]+)"', h)
print('  index panels (%d): %s' % (len(ids), ids))

print()
print('=== 后端路由 ===')
routes = sorted(set(re.findall(r'route == "(/api/[a-z_/]*)"', s)))
print('  共 %d 个: %s' % (len(routes), ', '.join(routes)))

print()
print('=== 导航结构（一级 key/label）===')
for m in re.finditer(r'\{\s*key:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    print('  %-14s %s' % (m.group(1), m.group(2)))
print('  --- 子标签 ---')
for m in re.finditer(r'\{\s*tab:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    print('  %-16s %s' % (m.group(1), m.group(2)))

print()
print('=== 数据量 ===')
con = sqlite3.connect('file:%s/viewer/app.db?mode=ro' % B, uri=True)
tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
for t in ['players', 'positions', 'trades', 'posts', 'news', 'lhb_stocks', 'lhb_seats', 'lhb_depts',
          'lhb_inst', 'lhb_sector_focus', 'stock_holders', 'stock_holder_num']:
    if t in tabs:
        print('  %-20s %s' % (t, con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]))
print()
print('  全部表:', tabs)

print()
print('=== 股东打标现状 ===')
cols = [r[1] for r in con.execute('PRAGMA table_info(stock_holders)')]
print('  列:', cols)
print('  已有标记列:', [c for c in cols if c in ('holder_kind', 'is_family', 'is_controlling', 'exclude_reason')])

print()
print('=== 板块扩散日期覆盖 ===')
try:
    for r in con.execute('SELECT trade_date, COUNT(*) n FROM lhb_sector_focus GROUP BY trade_date ORDER BY trade_date'):
        print('  %s  %s' % (r[0], r[1]))
except Exception as e:
    print('  ERR', e)
print('  lhb_stocks 最新日:', con.execute('SELECT MAX(trade_date) FROM lhb_stocks').fetchone()[0])
con.close()

print()
print('=== 根目录遗留临时文件 ===')
tmp = [f for f in os.listdir(B) if f.startswith('_') and os.path.isfile(os.path.join(B, f))]
print('  %d 个: %s' % (len(tmp), sorted(tmp)[:40]))
