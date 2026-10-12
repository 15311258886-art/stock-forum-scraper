# -*- coding: utf-8 -*-
import sys, os, re
sys.stdout.reconfigure(encoding='utf-8')

B = os.getcwd()
js_path = os.path.join(B, 'viewer', 'static', 'app.js')
js = open(js_path, encoding='utf-8').read()

print('=== 1) 当前 NAV 定义 ===')
i = js.find('const NAV')
j = js.find('\n];', i)
print(js[i:j+3] if i >= 0 else 'NAV NOT FOUND')
print()

print('=== 2) _nav_merge.py 状态 ===')
p = os.path.join(B, '_nav_merge.py')
print('  exists:', os.path.exists(p), (os.path.getsize(p) if os.path.exists(p) else ''))
if os.path.exists(p):
    print('----- 脚本内容 -----')
    print(open(p, encoding='utf-8').read()[:5000])
