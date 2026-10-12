# -*- coding: utf-8 -*-
"""看清「实盘操作」下各板块实际展示什么，作为归拢依据。"""
import os, re

B = os.path.dirname(os.path.abspath(__file__))
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
lines = js.splitlines()

out = []
A = out.append

for fn in ['renderOperating', 'renderPlayers', 'renderNiusan', 'renderStarMoves',
           'renderPersons', 'renderConsensus', 'renderCohold', 'renderLhb', 'renderLhbSectors']:
    idx = None
    for n, l in enumerate(lines, 1):
        if re.match(r'^(async )?function %s\b' % fn, l):
            idx = n
            break
    A('=' * 72)
    A('%s  @line %s' % (fn, idx))
    A('=' * 72)
    if not idx:
        A('  NOT FOUND')
        A('')
        continue
    # 打印函数前 34 行（含说明文案）
    for i in range(idx - 1, min(idx + 34, len(lines))):
        s = lines[i]
        if s.strip():
            A('%5d: %s' % (i + 1, s[:168]))
    A('')

# 各 tab 的加载函数
A('=' * 72)
A('加载函数与接口')
A('=' * 72)
for n, l in enumerate(lines, 1):
    if re.search(r'^(async )?function (loadDashboard|loadNiusan|loadStarMoves|loadLhbSectors|loadLhb|loadConsensus|loadCohold)\b', l):
        A('%5d: %s' % (n, l.strip()[:150]))
        for i in range(n, min(n + 8, len(lines))):
            s = lines[i]
            if s.strip() and not s.strip().startswith('}'):
                A('%5d: %s' % (i + 1, s[:150]))
        A('')

print('\n'.join(out))
open(os.path.join(B, '_render_audit_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
