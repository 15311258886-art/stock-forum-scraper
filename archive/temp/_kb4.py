# -*- coding: utf-8 -*-
"""摸清学习板块接线点 + 共同持仓面板结构"""
import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = os.path.dirname(os.path.abspath(__file__))
os.chdir(B)

js = open('viewer/static/app.js', encoding='utf-8').read()
ls = js.splitlines()
print('app.js TOTAL', len(ls))
print()

print('=== 1) NAV "学习" 组 ===')
i = js.find('const NAV')
j = js.find('\n];', i)
nav = js[i:j+3]
print(nav)
print()

print('=== 2) learnSubItems / 学习相关函数 ===')
for n, l in enumerate(ls, 1):
    if re.search(r'learnSubItems|renderLearn|loadKb|ensureKbDoc|kb_doc|api/kb|学习|基础知识|指标', l, re.I):
        print('%5d: %s' % (n, l.rstrip()[:180]))
print()

print('=== 3) 按需渲染分支（selectNav 内 if (tab === ...)） ===')
for n, l in enumerate(ls, 1):
    if re.search(r"if \(tab === \"", l):
        print('%5d: %s' % (n, l.strip()[:170]))
print()

print('=== 4) index.html 面板清单 ===')
h = open('viewer/static/index.html', encoding='utf-8').read()
ids = [m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)]
print(ids)
print()

print('=== 5) ensurePanel 实现 ===')
k = js.find('function ensurePanel')
print(js[k:k+900] if k >= 0 else 'NOT FOUND')
