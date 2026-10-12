# -*- coding: utf-8 -*-
"""核查学习/知识库板块的结构与实现"""
import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
h = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()
srv = open(os.path.join(B, 'viewer', 'server.py'), encoding='utf-8').read()

print('app.js lines', len(js.splitlines()))
print()
print('=== NAV 全结构 ===')
i = js.find('const NAV')
j = js.find('\n];', i)
print(js[i:j + 3])
print()
print('=== index.html panels ===')
ids = [m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)]
print(len(ids), ids)
print()
print('=== 知识库相关函数/接口 ===')
for k in ['kb_list', 'kb_doc', 'basics', '基础知识', 'renderKb', 'loadKb', 'learn', '牛博士', 'zdx']:
    print('  %-14s js=%-4d srv=%-4d html=%-4d' % (k, js.count(k), srv.count(k), h.count(k)))
print()
print('=== 含 kb / learn / zdx 的行 ===')
for n, l in enumerate(js.splitlines(), 1):
    if re.search(r'kb_|renderKb|loadKb|zdx|learn_|牛博士|学习', l, re.I):
        print('%5d: %s' % (n, l.rstrip()[:170]))
