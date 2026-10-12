# -*- coding: utf-8 -*-
"""导出台看板前端接线所需的锚点与模板"""
import os, re, sys

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(BASE)
OUT = os.path.join(BASE, '_s7_out.txt')
buf = []
def P(*a):
    buf.append(' '.join(str(x) for x in a))

js = open('viewer/static/app.js', encoding='utf-8').read()
html = open('viewer/static/index.html', encoding='utf-8').read()
srv = open('viewer/server.py', encoding='utf-8').read()
jsl = js.splitlines()

P('===== 1. 接线状态 =====')
for k in ['dashboard', 'renderDashboard', 'loadDashboard', 'p-dashboard', 'DASH']:
    P('  app.js %-20s %d' % (k, js.count(k)))
P('  index p-dashboard:', 'p-dashboard' in html)
P('  server def api_dashboard:', srv.count('def api_dashboard'), '| route:', srv.count('route == "/api/dashboard"'))
P('')

P('===== 2. NAV 一级分组 =====')
for m in re.finditer(r'\{\s*key:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    P('  key=%-14s label=%s' % (m.group(1), m.group(2)))
P('')

P('===== 3. NAV 完整定义 =====')
i = js.find('const NAV')
if i > 0:
    j = js.find('\n];', i)
    P(js[i:j+3])
P('')

P('===== 4. index.html panel 区 =====')
for n, l in enumerate(html.splitlines(), 1):
    if 'class="panel"' in l:
        P('  %5d: %s' % (n, l.strip()[:150]))
P('')

P('===== 5. loadAll 定义 =====')
i = js.find('function loadAll')
if i > 0:
    P(js[i:i+1400])
P('')

P('===== 6. api() / 辅助函数 =====')
for name in ['function api(', 'function amtYi', 'function setHint', 'function escAttr',
             'function esc(', 'function reportLoadError', 'function apiErrText', 'function openStock',
             'function lhbSymChip', 'function $$(', 'function $(']:
    i = js.find(name)
    P('  %-26s %s' % (name, ('@%d' % i) if i > 0 else 'NOT FOUND'))
P('')

P('===== 7. renderLhbSectors（渲染模板参考）=====')
i = js.find('function renderLhbSectors')
if i > 0:
    P(js[i:i+2600])
P('')

P('===== 8. loadLhbSectors / LBHS 定义 =====')
for pat in [r'const LBHS[^\n]*', r'function loadLhbSectors[^\n]*']:
    for m in re.finditer(pat, js):
        P('  ', m.group(0)[:150])
P('')

P('===== 9. 按需渲染分支（selectNav 尾部）=====')
i = js.find('if (tab === "lhb_sectors")')
if i > 0:
    P(js[max(0, i-700):i+700])
P('')

P('===== 10. api_dashboard 返回结构 =====')
i = srv.find('def api_dashboard')
j = srv.find('def api_summary', i)
P(srv[i:j])
P('')

P('===== 11. 路由分发 /api/dashboard =====')
i = srv.find('route == "/api/dashboard"')
if i > 0:
    P(srv[max(0, i-400):i+400])
P('')

open(OUT, 'w', encoding='utf-8').write('\n'.join(buf))
print('WROTE', OUT, os.path.getsize(OUT))
