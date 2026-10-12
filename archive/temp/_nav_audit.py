# -*- coding: utf-8 -*-
"""盘点「实盘操作」一级下的子标签结构 + 各板块后端数据来源，作为归拢依据。"""
import os, re, json

B = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(B, 'viewer', 'static', 'app.js')
HTML = os.path.join(B, 'viewer', 'static', 'index.html')
SRV = os.path.join(B, 'viewer', 'server.py')

js = open(JS, encoding='utf-8').read()
html = open(HTML, encoding='utf-8').read()
srv = open(SRV, encoding='utf-8').read()

out = []
A = out.append

# ---------- 1. NAV 全定义 ----------
A('=' * 70)
A('1) NAV 全定义')
A('=' * 70)
i = js.find('const NAV')
if i < 0:
    i = js.find('NAV =')
seg = js[i:i + 4200]
A(seg)

# ---------- 2. 一级分组 key/label ----------
A('')
A('=' * 70)
A('2) 一级分组')
A('=' * 70)
for m in re.finditer(r'\{\s*key:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    A('  %-18s %s' % (m.group(1), m.group(2)))

# ---------- 3. 子标签 ----------
A('')
A('=' * 70)
A('3) 子标签（tab/label）')
A('=' * 70)
for m in re.finditer(r'tab:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    A('  %-18s %s' % (m.group(1), m.group(2)))

# ---------- 4. 面板 id ----------
A('')
A('=' * 70)
A('4) index.html 面板 id')
A('=' * 70)
ids = [m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', html)]
A('  共 %d 个: %s' % (len(ids), ', '.join(ids)))

# ---------- 5. 各 tab 的加载/渲染函数 ----------
A('')
A('=' * 70)
A('5) tab -> 渲染函数 映射')
A('=' * 70)
for m in re.finditer(r'if \(tab === "([a-z_]+)"\)\s*\{?\s*(?:if[^\n]*\n\s*)?([^\n]*)', js):
    A('  %-18s %s' % (m.group(1), m.group(2).strip()[:120]))

# ---------- 6. 后端接口 -> 表 ----------
A('')
A('=' * 70)
A('6) 相关接口函数 -> 使用的表')
A('=' * 70)
tabs = ['lhb', 'lhb_sectors', 'niusan', 'star_moves', 'hold_cohold',
        'dashboard', 'players', 'positions', 'trades', 'topics']
for name in tabs:
    key = 'def api_' + name
    i = srv.find(key)
    if i < 0:
        A('  %-16s NOT FOUND' % name)
        continue
    j = srv.find('\ndef ', i + 10)
    body = srv[i:j if j > 0 else i + 4000]
    tables = sorted(set(re.findall(r'\b(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_]{3,30})', body, re.I)))
    A('  %-16s %s' % (name, ', '.join(tables) if tables else '(内存/无表)'))
    A('      行数 %d | 首行: %s' % (len(body.splitlines()), body.splitlines()[0][:90] if body else ''))

print('\n'.join(out))
open(os.path.join(B, '_nav_audit_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
