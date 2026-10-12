# -*- coding: utf-8 -*-
"""读 renderDashboard 全文 + 校验类名与CSS匹配"""
import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = os.getcwd()
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
i = js.find('function renderDashboard')
m = re.search(r'\nfunction [a-zA-Z]', js[i+10:])
j = i + 10 + m.start() + 1 if m else i + 6000
seg = js[i:j]
print('SPAN %d-%d  LEN %d' % (i, j, len(seg)))
print(seg)
print()
css = open(os.path.join(B, 'viewer', 'static', 'app.css'), encoding='utf-8').read()
used = sorted(set(re.findall(r'\b(ml-[a-z0-9-]+|lv-[a-z]+)\b', seg)))
defined = sorted(set(re.findall(r'\.(ml-[a-z0-9-]+|lv-[a-z]+)', css)))
print('=== 渲染用到的类 ===')
print(' ', used)
print('=== CSS 已定义 ===')
print(' ', defined)
print('=== 缺失定义 ===')
print(' ', [u for u in used if u not in defined])
