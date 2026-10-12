"""核对：后端已实现但前端未调用的接口；以及各缺口的具体影响面。"""
import os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
srv = open(os.path.join(B, 'viewer', 'server.py'), encoding='utf-8').read()
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()

routes = sorted(set(re.findall(r'route == "(/api/[a-z_/]*)"', srv)))
used = set(re.findall(r'api\("(/api/[a-z_/]+)', js))
# 动态拼接的也要算（如 "/api/player?name=" 之类）
used_loose = set()
for m in re.finditer(r'"/api/([a-z_/]+)', js):
    used_loose.add('/api/' + m.group(1))

print('===== 后端有、前端未直连的接口 =====')
unused = [r for r in routes if r not in used_loose]
for r in unused:
    print('  ', r)
print('  合计', len(unused), '/', len(routes))

print()
print('===== 前端 NAV 子标签 → 是否有渲染函数 =====')
tabs = re.findall(r'\{\s*tab:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js)
for tab, label in tabs:
    fn = 'render' + tab.title().replace('_', '')
    cands = [n for n in ('render' + ''.join(p.capitalize() for p in tab.split('_')),
                         'render' + tab.upper()) if n in js]
    print('  %-20s %-14s %s' % (tab, label, cands or '（未见同名函数）'))

print()
print('===== 板块扩散缺口 =====')
import sqlite3
con = sqlite3.connect('file:%s?mode=ro' % os.path.join(B, 'viewer', 'app.db').replace('\\', '/'), uri=True)
print('  lhb_stocks 有榜的日期:', [r[0] for r in con.execute('SELECT DISTINCT trade_date FROM lhb_stocks ORDER BY trade_date')])
print('  sector_focus 已算的日期:', [r[0] for r in con.execute('SELECT DISTINCT trade_date FROM lhb_sector_focus ORDER BY trade_date')])
con.close()

print()
print('===== 表：positions 时间填充（判断能否标注购买时间）=====')
con = sqlite3.connect('file:%s?mode=ro' % os.path.join(B, 'viewer', 'app.db').replace('\\', '/'), uri=True)
for r in con.execute("SELECT site, COUNT(*) n, SUM(CASE WHEN post_time IS NULL OR TRIM(post_time)='' THEN 1 ELSE 0 END) blank FROM positions GROUP BY site"):
    print('  %-10s n=%-4s 无时间=%s' % (r[0], r[1], r[2]))
con.close()

print()
print('===== 股东变动字段可用量（对标"新进/增持/减持"告警）=====')
con = sqlite3.connect('file:%s?mode=ro' % os.path.join(B, 'viewer', 'app.db').replace('\\', '/'), uri=True)
for r in con.execute("SELECT hold_change, COUNT(*) n FROM stock_holders GROUP BY hold_change ORDER BY n DESC LIMIT 8"):
    print('  %r  %s' % (r[0], r[1]))
con.close()
