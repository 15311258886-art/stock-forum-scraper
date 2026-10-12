# -*- coding: utf-8 -*-
"""导出共识相关：后端 api_consensus / api_cohold_recent + 前端 renderConsensus + 路由"""
import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')

B = os.path.dirname(os.path.abspath(__file__))
s = open(os.path.join(B, 'viewer', 'server.py'), encoding='utf-8').read()
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()

out = []
out.append('=========== 后端 api_consensus ===========')
i = s.find('def api_consensus')
if i >= 0:
    j = s.find('\ndef ', i + 10)
    out.append(s[i:j if j > 0 else i + 4000])
else:
    out.append('NOT FOUND')

out.append('')
out.append('=========== 后端 api_cohold_recent ===========')
i2 = s.find('def api_cohold_recent')
if i2 >= 0:
    j2 = s.find('\ndef ', i2 + 10)
    out.append(s[i2:j2 if j2 > 0 else i2 + 4000])
else:
    out.append('NOT FOUND')

out.append('')
out.append('=========== 路由（consensus / cohold） ===========')
for m in re.finditer(r'route == "(/api/(?:consensus|cohold)[^"]*)"', s):
    k = m.start()
    out.append(s[max(0, k - 80):k + 260])
    out.append('-----')

out.append('')
out.append('=========== 前端 renderConsensus ===========')
i3 = js.find('function renderConsensus')
if i3 >= 0:
    m3 = re.search(r'\nfunction [a-zA-Z]', js[i3 + 10:])
    j3 = i3 + 10 + m3.start() + 1 if m3 else i3 + 4000
    out.append(js[i3:j3])
else:
    out.append('NOT FOUND')

out.append('')
out.append('=========== 前端 loadConsensus / 面板 id 引用 ===========')
for n, l in enumerate(js.splitlines(), 1):
    if re.search(r'loadConsensus|p-consensus|p-cohold|COHOLD|CONSENSUS', l):
        out.append('%5d: %s' % (n, l.rstrip()[:180]))

open(os.path.join(B, '_z_cs_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('WROTE', os.path.getsize(os.path.join(B, '_z_cs_out.txt')))
