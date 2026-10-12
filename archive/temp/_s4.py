# -*- coding: utf-8 -*-
"""导出看板接线锚点：NAV 一级/二级、面板、按需渲染、加载模板。"""
import os, re, io, sys

BASE = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(BASE, 'viewer', 'static', 'app.js')
HTML = os.path.join(BASE, 'viewer', 'static', 'index.html')
OUT = os.path.join(BASE, '_s4_out.txt')

buf = []
def p(*a):
    buf.append(' '.join(str(x) for x in a))

js = open(JS, encoding='utf-8').read()
h = open(HTML, encoding='utf-8').read()
jsl = js.splitlines()

p('=== dashboard 接线状态 ===')
for k in ['dashboard', 'renderDashboard', 'loadDashboard', 'p-dashboard', '看板']:
    p('  %-18s %d' % (k, js.count(k)))
p('  index 含 p-dashboard: %s' % ('p-dashboard' in h))

p('')
p('=== NAV 一级分组（key/label） ===')
for m in re.finditer(r'\{\s*key:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    p('  key=%-16s label=%s' % (m.group(1), m.group(2)))

p('')
p('=== NAV 全量文本（前 4200 字） ===')
i = js.find('const NAV')
p(js[i:i+4200] if i >= 0 else 'NAV NOT FOUND')

p('')
p('=== index.html 面板 id ===')
p('  ' + str([m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)]))

p('')
p('=== 按需渲染分支 selectNav ===')
i = js.find('function selectNav')
if i < 0:
    i = js.find('if (tab === "lhb_sectors")')
p(js[max(0, i-300):i+1400] if i >= 0 else 'NOT FOUND')

p('')
p('=== loadAll / 初始加载 ===')
for n, l in enumerate(jsl, 1):
    if re.search(r'function loadAll|loadLhb\(\)|loadLhbSectors\(\)|api\("/api/summary"', l):
        p('  %5d: %s' % (n, l.rstrip()[:170]))

p('')
p('=== renderLhbSectors 头部（作为渲染模板） ===')
i = js.find('function renderLhbSectors')
p(js[i:i+1200] if i >= 0 else 'NOT FOUND')

open(OUT, 'w', encoding='utf-8').write('\n'.join(buf))
print('WROTE', OUT, len('\n'.join(buf)))
