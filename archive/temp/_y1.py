# -*- coding: utf-8 -*-
"""精确导出导航归拢所需的全部代码段"""
import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')

B = os.path.dirname(os.path.abspath(__file__))
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
h  = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()

print('#' * 70)
print('1) NAV 定义')
print('#' * 70)
i = js.find('const NAV')
j = js.find('\n];', i)
print(js[i:j+3])

print()
print('#' * 70)
print('2) 子导航渲染 / 切换（含 selectNav）')
print('#' * 70)
ls = js.splitlines()
for n, l in enumerate(ls, 1):
    if re.search(r'function (renderSubNav|renderNav|selectNav|ensurePanel|navigate)\b', l):
        print('ANCHOR %5d: %s' % (n, l.strip()[:140]))
print()
# 打印 selectNav 全文
i2 = js.find('function selectNav')
if i2 < 0:
    i2 = js.find('function navigate')
if i2 >= 0:
    m = re.search(r'\nfunction [a-zA-Z]', js[i2+10:])
    j2 = i2 + 10 + m.start() + 1 if m else i2 + 3000
    print(js[i2:j2])

print()
print('#' * 70)
print('3) index.html 面板 id')
print('#' * 70)
print([m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)])
print()
# 面板行
for n, l in enumerate(h.splitlines(), 1):
    if 'class="panel"' in l:
        print('%5d: %s' % (n, l.strip()[:110]))

print()
print('#' * 70)
print('4) 关键函数定位')
print('#' * 70)
for n, l in enumerate(ls, 1):
    if re.match(r'^(async )?function (renderNiusan|loadNiusan|renderStarMoves|loadStarMoves)\b', l) or re.match(r'^const (NIU|STARM)\b', l):
        print('%5d: %s' % (n, l.strip()[:150]))

print()
print('#' * 70)
print('5) renderNiusan 全文')
print('#' * 70)
i3 = js.find('function renderNiusan')
if i3 >= 0:
    m = re.search(r'\nfunction [a-zA-Z]', js[i3+10:])
    j3 = i3 + 10 + m.start() + 1 if m else i3 + 4000
    print('--- span %d-%d ---' % (i3, j3))
    print(js[i3:j3])

print()
print('#' * 70)
print('6) renderStarMoves 全文')
print('#' * 70)
i4 = js.find('function renderStarMoves')
if i4 >= 0:
    m = re.search(r'\nfunction [a-zA-Z]', js[i4+10:])
    j4 = i4 + 10 + m.start() + 1 if m else i4 + 4000
    print('--- span %d-%d ---' % (i4, j4))
    print(js[i4:j4])

print()
print('#' * 70)
print('7) loadNiusan / loadStarMoves 全文')
print('#' * 70)
for fn in ('loadNiusan', 'loadStarMoves'):
    i5 = js.find('function ' + fn)
    if i5 < 0:
        print(fn, 'NOT FOUND'); continue
    m = re.search(r'\nfunction [a-zA-Z]', js[i5+10:])
    j5 = i5 + 10 + m.start() + 1 if m else i5 + 900
    print('--- %s ---' % fn)
    print(js[i5:j5])
