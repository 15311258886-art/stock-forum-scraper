# -*- coding: utf-8 -*-
"""读 summary 接口 + 前端 summary 渲染 + 默认首页逻辑，为结构重组提供依据。"""
import os, re, sqlite3, json

BASE = os.path.dirname(os.path.abspath(__file__))

print('=' * 70)
print('[1] /api/summary 实现')
print('=' * 70)
srv = open(os.path.join(BASE, 'viewer', 'server.py'), encoding='utf-8').read()
i = srv.find('def api_summary')
j = srv.find('\ndef ', i + 10)
print(srv[i:j][:3000])

print()
print('=' * 70)
print('[2] 前端默认 tab / 初始化')
print('=' * 70)
js = open(os.path.join(BASE, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
ls = js.splitlines()
for n, l in enumerate(ls, 1):
    if re.search(r'STATE\s*=|currentTab|activeTab|location\.hash|DEFAULT_TAB|selectNav\(|function boot|loadAll\(\)', l):
        print('  %5d: %s' % (n, l.strip()[:150]))

print()
print('=' * 70)
print('[3] 前端 summary 渲染函数')
print('=' * 70)
for n, l in enumerate(ls, 1):
    if re.search(r'^function renderSummary|^function renderSourcesPanel|^function ensurePanel', l):
        print('  %5d: %s' % (n, l.strip()[:130]))

print()
print('=' * 70)
print('[4] 可用素材：各源最新数据时间')
print('=' * 70)
con = sqlite3.connect('file:%s?mode=ro' % os.path.join(BASE, 'viewer', 'app.db').replace('\\', '/'), uri=True)
con.row_factory = sqlite3.Row
def q(sql, label):
    try:
        r = con.execute(sql).fetchone()
        print('  %-24s %s' % (label, tuple(r)))
    except Exception as e:
        print('  %-24s ERR %s' % (label, str(e)[:60]))
q("SELECT COUNT(*), MAX(trade_date) FROM lhb_stocks", 'lhb_stocks')
q("SELECT COUNT(*), MAX(post_time) FROM trades", 'trades(有时间)')
q("SELECT COUNT(*), MAX(post_time) FROM posts", 'posts')
q("SELECT COUNT(*), MAX(fetched_at) FROM raw_pages", 'raw_pages')
q("SELECT COUNT(*), MAX(publish_time) FROM news", 'news')
q("SELECT COUNT(DISTINCT symbol), MAX(end_date) FROM stock_holders", 'stock_holders')
q("SELECT COUNT(*), MAX(post_time) FROM positions", 'positions')
con.close()
