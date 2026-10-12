# -*- coding: utf-8 -*-
"""导出待整合面板的渲染实现，判断合并可行性。"""
import os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
ls = js.splitlines()

out = []
out.append('=== 函数行号索引 ===')
for i, l in enumerate(ls, 1):
    if re.match(r'^(async )?function (render|load)(Persons|Operating|Consensus|Cohold|Niusan|StarMoves|Lhb|LhbSectors|Players)\b', l):
        out.append('%5d: %s' % (i, l.strip()[:130]))
out.append('')

# 导出几个关键函数的头部（看它渲染什么）
for fn in ['renderPersons', 'renderOperating', 'renderConsensus', 'renderCohold']:
    m = re.search(r'^(async )?function ' + fn + r'\b', js, re.M)
    if not m:
        out.append('--- %s NOT FOUND ---' % fn)
        continue
    start = m.start()
    # 截取到下一个顶层 function
    nxt = re.search(r'\n(?:async )?function \w+', js[start + 10:])
    body = js[start: start + 10 + (nxt.start() if nxt else 2500)]
    out.append('=== %s (%d 字符) ===' % (fn, len(body)))
    out.append(body[:2200])
    out.append('')

open(os.path.join(B, '_panels_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out)[:12000])
