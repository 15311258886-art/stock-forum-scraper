# -*- coding: utf-8 -*-
"""一次取准：共识面板 + 学习板块 的接线点"""
import sys, re, os, json
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)

out = []

# ---------- 1) 后端 api_consensus ----------
s = open('viewer/server.py', encoding='utf-8').read()
i = s.find('def api_consensus')
j = s.find('\ndef ', i + 10)
out.append('===== api_consensus (span %d-%d) =====' % (i, j))
out.append(s[i:j] if i >= 0 else 'NOT FOUND')
out.append('')

out.append('===== consensus 路由 =====')
for m in re.finditer(r'route == "/api/consensus[^"]*"', s):
    k = m.start()
    out.append(s[max(0, k - 150):k + 400])
    out.append('---')

# ---------- 2) 前端 renderConsensus ----------
js = open('viewer/static/app.js', encoding='utf-8').read()
ls = js.splitlines()
out.append('')
out.append('===== renderConsensus / loadConsensus =====')
for n, l in enumerate(ls, 1):
    if re.match(r'^(async )?function (renderConsensus|loadConsensus|renderCohold|loadCohold)\b', l):
        out.append('%5d: %s' % (n, l.strip()[:150]))

i = js.find('function renderConsensus')
if i >= 0:
    m = re.search(r'\nfunction [a-zA-Z]', js[i + 10:])
    j2 = i + 10 + m.start() + 1 if m else i + 3000
    out.append('')
    out.append('----- renderConsensus 全文 (span %d-%d) -----' % (i, j2))
    out.append(js[i:j2])

i = js.find('function loadConsensus')
if i >= 0:
    m = re.search(r'\nfunction [a-zA-Z]', js[i + 10:])
    j2 = i + 10 + m.start() + 1 if m else i + 800
    out.append('')
    out.append('----- loadConsensus -----')
    out.append(js[i:j2])

# ---------- 3) 学习板块 ----------
out.append('')
out.append('===== 学习板块相关行 =====')
for n, l in enumerate(ls, 1):
    if re.search(r'learn|学习|zdx|EDU\b|renderLearn|edu-', l, re.I):
        out.append('%5d: %s' % (n, l.rstrip()[:180]))

# ---------- 4) NAV ----------
out.append('')
out.append('===== NAV =====')
i = js.find('const NAV')
j2 = js.find('\n];', i)
out.append(js[i:j2 + 3])

# ---------- 5) index.html 面板 ----------
h = open('viewer/static/index.html', encoding='utf-8').read()
out.append('')
out.append('===== index.html panel ids =====')
out.append(', '.join([m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)]))

open('_pk1_out.txt', 'w', encoding='utf-8').write('\n'.join(out))
print('written', len('\n'.join(out)))
