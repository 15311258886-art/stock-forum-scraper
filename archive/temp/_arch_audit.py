# -*- coding: utf-8 -*-
"""信息架构盘点：表 / 接口 / 导航 三层，为结构优化提供基准。"""
import os, re, sqlite3, json

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'viewer', 'app.db')

print('=' * 70)
print('[1] 数据层：业务表清单与量级')
print('=' * 70)
con = sqlite3.connect('file:%s?mode=ro' % DB.replace('\\', '/'), uri=True)
tabs = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
skip = ('sqlite_',)
rows = []
for t in tabs:
    try:
        n = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
    except Exception:
        n = -1
    rows.append((t, n))
for t, n in rows:
    if n > 0:
        print('  %-28s %s' % (t, n))
print('  --- 空表 ---')
print(' ', [t for t, n in rows if n == 0] or '无')

print()
print('=' * 70)
print('[2] 牛散数据现状')
print('=' * 70)
try:
    tot = con.execute('SELECT COUNT(*) FROM stock_holders').fetchone()[0]
    syms = con.execute('SELECT COUNT(DISTINCT symbol) FROM stock_holders').fetchone()[0]
    nat = con.execute("SELECT COUNT(*) FROM stock_holders WHERE holder_type='自然人'").fetchone()[0]
    stars = con.execute("""SELECT COUNT(*) FROM (
      SELECT holder_name FROM stock_holders WHERE holder_type='自然人'
      GROUP BY holder_name HAVING COUNT(DISTINCT symbol)>=2)""").fetchone()[0]
    print('  stock_holders 行=%s 覆盖股=%s 自然人条=%s 牛散候选(跨>=2股)=%s'
          % (tot, syms, nat, stars))
    r = con.execute("SELECT MIN(end_date), MAX(end_date) FROM stock_holders").fetchone()
    print('  报告期范围: %s ~ %s' % r)
except Exception as e:
    print('  ERR', e)
con.close()

print()
print('=' * 70)
print('[3] 接口层：API 路由清单')
print('=' * 70)
srv = open(os.path.join(BASE, 'viewer', 'server.py'), encoding='utf-8').read()
routes = sorted(set(re.findall(r'route == "(/api/[a-z_/]*)"', srv)))
print('  共 %d 个：' % len(routes))
for r in routes:
    print('    ', r)

print()
print('=' * 70)
print('[4] 前端导航结构（信息架构）')
print('=' * 70)
js = open(os.path.join(BASE, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
i = js.find('const NAV')
j = js.find('];', i)
print(js[i:j + 2] if i > 0 else 'NAV NOT FOUND')

print()
print('=' * 70)
print('[5] 前端面板容器清单')
print('=' * 70)
html = open(os.path.join(BASE, 'viewer', 'static', 'index.html'), encoding='utf-8').read()
ids = [m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', html)]
print('  %d 个: %s' % (len(ids), ids))
