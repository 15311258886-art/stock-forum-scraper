# -*- coding: utf-8 -*-
"""导出 api_dashboard 源码 + 渲染模板参考"""
import os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)

s = open('viewer/server.py', encoding='utf-8').read()
i = s.find('def api_dashboard')
j = s.find('def api_summary', i)
open('_dsrc.txt', 'w', encoding='utf-8').write(s[i:j])
print('api_dashboard LEN', j - i)

js = open('viewer/static/app.js', encoding='utf-8').read()

# 1) renderLhbSectors 全文（作为渲染模板）
a = js.find('function renderLhbSectors')
b = js.find('\nfunction ', a + 10)
print('\n=== renderLhbSectors ===')
print(js[a:b][:3000])

# 2) LBHS 状态变量 + load 函数
print('\n=== LBHS 状态与 load ===')
for n, l in enumerate(js.splitlines(), 1):
    if re.search(r'const (LBHS|LHB_SEC|NS|NIU)\b|function loadLhbSectors|function loadNiusan|async function loadNiusan', l):
        print('%5d: %s' % (n, l.strip()[:150]))

# 3) 按需渲染分支 + loadAll
print('\n=== 按需渲染分支 ===')
for n, l in enumerate(js.splitlines(), 1):
    if re.search(r'if \(tab === "', l):
        print('%5d: %s' % (n, l.strip()[:150]))

print('\n=== loadAll 定义 ===')
i2 = js.find('function loadAll')
print(js[i2:i2+1400] if i2 > 0 else 'NOT FOUND')

# 4) 辅助函数
print('\n=== 辅助函数定义 ===')
for n, l in enumerate(js.splitlines(), 1):
    if re.match(r'^(async )?function (amtYi|setHint|escAttr|esc|reportLoadError|openStock|lhbPct|lhbRate|api|fmtTimeText)\b', l):
        print('%5d: %s' % (n, l.strip()[:130]))
