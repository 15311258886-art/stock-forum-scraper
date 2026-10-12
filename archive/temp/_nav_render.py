# -*- coding: utf-8 -*-
"""定位 NAV 定义与渲染逻辑，判断二级分组可行性。"""
import os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
ls = js.splitlines()

out = []
out.append('TOTAL %d' % len(ls))
out.append('')
out.append('=== 所有 NAV 相关引用 ===')
for i, l in enumerate(ls, 1):
    if re.search(r'\bNAV\b|nav-item|nav-group|navGroup|sub-tab|subTab|data-tab|dataset\.tab', l):
        out.append('%5d: %s' % (i, l.rstrip()[:175]))
out.append('')
out.append('=== selectNav / switchTab 类函数 ===')
for i, l in enumerate(ls, 1):
    if re.match(r'^(async )?function \w*(Nav|Tab|Panel|View)\w*\(', l, re.I):
        out.append('%5d: %s' % (i, l.strip()[:140]))

open(os.path.join(B, '_nav_render.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out)[:9000])
