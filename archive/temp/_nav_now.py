# -*- coding: utf-8 -*-
"""导出当前 NAV 结构 + 各子标签的渲染函数 + 面板 id，用于归拢决策。"""
import os, re, io, sys

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
html = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()
ls = js.splitlines()

out = []
out.append('================ NAV 定义（原文） ================')
i = js.find('const NAV')
if i < 0:
    i = js.find('NAV =')
if i >= 0:
    j = js.find('];', i)
    out.append(js[i:j + 2])
out.append('')

out.append('================ index.html 面板 id ================')
ids = [m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', html)]
out.append('共 %d 个: %s' % (len(ids), ', '.join(ids)))
out.append('')

out.append('================ 各板块 render/load 函数 ================')
for n, l in enumerate(ls, 1):
    if re.match(r'^(async )?function (render|load)[A-Z]\w*\(', l):
        out.append('%5d: %s' % (n, l.strip()[:140]))
out.append('')

out.append('================ 子导航渲染逻辑 ================')
for n, l in enumerate(ls, 1):
    if re.search(r'\.sub-tab|subTab|renderSubNav|items\.map|group\.items|cat\.items', l):
        out.append('%5d: %s' % (n, l.rstrip()[:160]))
out.append('')

out.append('================ loadAll / 按需渲染分支 ================')
i = js.find('function loadAll')
if i >= 0:
    out.append(js[i:i + 1600])
out.append('')
for n, l in enumerate(ls, 1):
    if 'if (tab ===' in l:
        out.append('%5d: %s' % (n, l.strip()[:140]))

open(os.path.join(B, '_nav_now_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('WROTE _nav_now_out.txt lines=%d' % len(out))
