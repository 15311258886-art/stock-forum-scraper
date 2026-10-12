# -*- coding: utf-8 -*-
"""导出 NIUSAN 常量定义 + 持仓 chip 渲染段"""
import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')

B = os.path.dirname(os.path.abspath(__file__))
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
ls = js.splitlines()

print('#' * 70)
print('1) NIUSAN / STARM / LBHS 常量定义')
print('#' * 70)
for n, l in enumerate(ls, 1):
    if re.match(r'^const (NIUSAN|STARM|LBHS|LHB|DASH|KB)\b', l):
        print('%5d: %s' % (n, l.rstrip()[:200]))

print()
print('#' * 70)
print('2) 持仓 chip 渲染段（含 hold-chip / chip-time）')
print('#' * 70)
idx = [n for n, l in enumerate(ls, 1) if 'hold-chip' in l or 'chip-time' in l or 'hlShown' in l]
print('命中行:', idx)
if idx:
    a = max(0, min(idx) - 30)
    b = min(len(ls), max(idx) + 12)
    for i in range(a, b):
        print('%5d: %s' % (i + 1, ls[i][:190]))

print()
print('#' * 70)
print('3) hls / holdings 相关渲染（api_players 前端）')
print('#' * 70)
i = js.find('const hls = toArr(p.holdings)')
if i < 0:
    i = js.find('toArr(p.holdings)')
print('at', i)
if i >= 0:
    print(js[max(0, i - 800): i + 2400])
