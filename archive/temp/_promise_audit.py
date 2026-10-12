# -*- coding: utf-8 -*-
"""核查：历轮要求中，哪些只做了数据层、未落到页面层。"""
import os, re, sys, json, sqlite3
sys.stdout.reconfigure(encoding='utf-8')

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
html = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()
css = open(os.path.join(B, 'viewer', 'static', 'app.css'), encoding='utf-8').read()
srv = open(os.path.join(B, 'viewer', 'server.py'), encoding='utf-8').read()

def cnt(s, k):
    return s.count(k)

print('=' * 70)
print('A. 主线研判（看板改成 方向→龙头→梯队→分支+推荐标记）')
print('=' * 70)
for k in ['mainlines', 'ml-card', 'ml-lead', 'ml-branch', 'ml-forward', 'ml-rating']:
    print('  app.js  %-14s %d' % (k, cnt(js, k)))
for k in ['.ml-card', '.ml-lead', '.ml-branch', '.ml-forward', '.ml-rating', 'ml-' ]:
    print('  app.css %-14s %d' % (k, cnt(css, k)))
print('  server.py mainlines %d' % cnt(srv, 'mainlines'))
print('  index p-dashboard  %s' % ('p-dashboard' in html))

print()
print('=' * 70)
print('B. 导航归拢（龙虎榜归到「行情研判」；实盘操作子标签收敛）')
print('=' * 70)
i = js.find('const NAV')
j = js.find('];', i)
nav = js[i:j+2] if i >= 0 else ''
# 找龙虎榜所在分组
for m in re.finditer(r'label:\s*"([^"]+)"[\s\S]{0,1200}?tab:\s*"(lhb|lhb_sectors|operating)"', nav):
    print('  group?', m.group(1), '->', m.group(2))
print('  --- NAV 原文（前 2500 字）---')
print(nav[:2500])

print()
print('=' * 70)
print('C. 噪声剔除（滞后>5天）是否用到页面层')
print('=' * 70)
for k in ['stale', 'excluded', 'noise', 'quality']:
    print('  app.js  %-10s %d' % (k, cnt(js, k)))
    print('  server  %-10s %d' % (k, cnt(srv, k)))

print()
print('=' * 70)
print('D. 持仓 chip 显示建仓日期（去掉「关注/操作」+「时间未披露」）')
print('=' * 70)
for k in ['chip-time', 'hold-chip', '关注/操作', '时间未披露', 'holdTimeTag', 'hold_since']:
    print('  app.js  %-14s %d' % (k, cnt(js, k)))
i = js.find('const hls = toArr(p.holdings)')
print('  --- chip 渲染段 ---')
print(js[max(0, i-200):i+1400] if i >= 0 else '  NOT FOUND')

print()
print('=' * 70)
print('E. 雪球人轴解析器是否接入')
print('=' * 70)
print('  xueqiu_person.py 存在:', os.path.exists(os.path.join(B, 'xueqiu_person.py')))
ing = open(os.path.join(B, 'viewer', 'ingest.py'), encoding='utf-8').read()
print('  ingest 引用 xueqiu_person:', cnt(ing, 'xueqiu_person'))
print('  ingest 引用 person:', cnt(ing, 'person'))

print()
print('=' * 70)
print('F. 牛散变动流 / 板块动向 / 龙虎榜 前端接线')
print('=' * 70)
for k in ['star_moves', 'renderStarMoves', 'lhb_sectors', 'renderLhbSectors', 'renderNiusan']:
    print('  app.js  %-18s %d' % (k, cnt(js, k)))
print('  panels:', re.findall(r'id="(p-[a-z_]+)"', html))

print()
print('=' * 70)
print('G. 数据层标记现状（DB）')
print('=' * 70)
con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row
for t, cols in [('players', ['site', 'axis']), ('positions', ['site', 'stale']),
                ('posts', ['site', 'stale']), ('trades', ['site', 'stale']), ('news', ['site', 'stale'])]:
    have = [r[1] for r in con.execute('PRAGMA table_info(%s)' % t)]
    miss = [c for c in cols if c not in have]
    print('  %-10s 缺列: %s' % (t, miss or '无'))
    if not miss:
        key = cols[-1]
        for r in con.execute('SELECT %s, COUNT(*) n FROM %s GROUP BY %s ORDER BY n DESC LIMIT 8' % (key, t, key)):
            print('     %-12s %s' % (r[0], r[1]))
try:
    print('  sector_chain:', con.execute('SELECT COUNT(*) FROM sector_chain').fetchone()[0], '条')
except Exception as e:
    print('  sector_chain: ERR', e)
try:
    print('  lhb_sector_focus:', con.execute('SELECT COUNT(*) FROM lhb_sector_focus').fetchone()[0], '条')
except Exception as e:
    print('  lhb_sector_focus: ERR', e)
con.close()
