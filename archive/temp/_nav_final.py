# -*- coding: utf-8 -*-
"""一次性导出：NAV 全结构 + 各 tab 的渲染函数 + 数据源表 + 面板 id"""
import os, re, io, sys

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
html = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()
server = open(os.path.join(B, 'viewer', 'server.py'), encoding='utf-8').read()
ls = js.splitlines()

print('=' * 70)
print('A) NAV 完整定义')
print('=' * 70)
i = js.find('const NAV')
j = js.find('\n];', i)
print(js[i:j + 3])

print()
print('=' * 70)
print('B) index.html 静态面板 id')
print('=' * 70)
print([m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', html)])

print()
print('=' * 70)
print('C) tab -> 渲染函数 / 数据变量 / 接口')
print('=' * 70)
# 找 selectNav 分发
k = js.find('function selectNav')
if k < 0:
    k = js.find('selectNav')
seg = js[k:k + 3000]
for m in re.finditer(r'if \(tab === "([a-z_]+)"\)[\s\S]{0,220}', seg):
    print('---', m.group(1))
    print(re.sub(r'\s+', ' ', m.group(0))[:230])

print()
print('=' * 70)
print('D) 各 render/load 函数定义行号')
print('=' * 70)
for n, l in enumerate(ls, 1):
    if re.match(r'^(async )?function (render|load)[A-Z]\w*\(', l):
        print('%5d: %s' % (n, l.strip()[:120]))

print()
print('=' * 70)
print('E) 后端接口 -> 数据表（按接口名）')
print('=' * 70)
for m in re.finditer(r'def (api_[a-z_]+)\(', server):
    name = m.group(1)
    seg2 = server[m.start():m.start() + 2600]
    tables = sorted(set(re.findall(r'FROM\s+([a-z_]+)', seg2)))
    print('  %-22s <- %s' % (name, ', '.join(tables) if tables else '-'))
