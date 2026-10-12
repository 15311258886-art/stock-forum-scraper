# -*- coding: utf-8 -*-
import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)

out = []
js = open('viewer/static/app.js', encoding='utf-8').read()
ls = js.splitlines()

out.append('===== learnSubItems 全文 =====')
i = js.find('function learnSubItems')
if i >= 0:
    m = re.search(r'\nfunction [a-zA-Z]', js[i + 10:])
    j = i + 10 + m.start() + 1 if m else i + 900
    out.append(js[i:j])

out.append('')
out.append('===== 按需渲染分支 (3405-3430) =====')
for n in range(3404, 3430):
    if n < len(ls):
        out.append('%5d: %s' % (n + 1, ls[n][:185]))

out.append('')
out.append('===== renderCohold 全文 (2266+) =====')
i = js.find('function renderCohold')
if i >= 0:
    m = re.search(r'\nfunction [a-zA-Z]', js[i + 10:])
    j = i + 10 + m.start() + 1 if m else i + 5000
    out.append('----- span %d-%d -----' % (i, j))
    out.append(js[i:min(j, i + 5500)])

# 后端：卖出聚合
out.append('')
out.append('===== server.py 中 SELL / sell 聚合 =====')
s = open('viewer/server.py', encoding='utf-8').read()
sl = s.splitlines()
for n, l in enumerate(sl, 1):
    if re.search(r'SELL|sell_side|"sell"|\'sell\'|def api_cohold_recent|def api_consensus|def consensus_stocks', l):
        out.append('%5d: %s' % (n, l.rstrip()[:170]))

out.append('')
out.append('===== api_cohold_recent 全文 =====')
i = s.find('def api_cohold_recent')
if i >= 0:
    m = re.search(r'\ndef ', s[i + 10:])
    j = i + 10 + m.start() + 1 if m else i + 6000
    out.append('----- span %d-%d -----' % (i, j))
    out.append(s[i:min(j, i + 6500)])

open('_pk2_out.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('written', len('\n'.join(out)))
