# -*- coding: utf-8 -*-
"""核查看板接线状态与NAV结构"""
import os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)

js = open('viewer/static/app.js', encoding='utf-8').read()
h = open('viewer/static/index.html', encoding='utf-8').read()
s = open('viewer/server.py', encoding='utf-8').read()

out = []
out.append('app.js lines: %d' % len(js.splitlines()))
out.append('')
out.append('=== 今日看板接线状态 ===')
for k in ['dashboard', 'renderDashboard', 'loadDashboard', 'p-dashboard', 'DASH']:
    out.append('  app.js %-18s %d' % (k, js.count(k)))
out.append('  index p-dashboard: %s' % ('p-dashboard' in h))
out.append('  server api_dashboard: %d | route: %d' % (s.count('def api_dashboard'), s.count('route == "/api/dashboard"')))
out.append('')
out.append('=== NAV 一级分组 ===')
for m in re.finditer(r'\{\s*key:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    out.append('  key=%-14s label=%s' % (m.group(1), m.group(2)))
out.append('')
out.append('=== NAV 定义首 30 行 ===')
i = js.find('const NAV')
for n, l in enumerate(js[i:i+1800].splitlines()[:34], 1):
    out.append('  %2d: %s' % (n, l[:170]))

open('_s5_out.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out))
