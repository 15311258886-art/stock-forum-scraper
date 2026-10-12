# -*- coding: utf-8 -*-
import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
ls = js.splitlines()
print('=== 3355-3440 (selectNav / renderSubNav / 默认落点) ===')
for i in range(3354, 3440):
    print('%5d: %s' % (i + 1, ls[i][:185]))
print()
print('=== index.html 中 learn 面板 ===')
h = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()
for n, l in enumerate(h.splitlines(), 1):
    if 'p-learn' in l or 'p-fetchlog' in l:
        print('%5d: %s' % (n, l.strip()[:150]))
print()
print('=== app.css 中 kb- 样式 ===')
css = open(os.path.join(B, 'viewer', 'static', 'app.css'), encoding='utf-8').read()
print('kb- count', css.count('kb-'))
for m in re.finditer(r'[^\n{}]*\.(kb-[a-z0-9-]+)[^{]*\{[^}]*\}', css):
    print('  ', re.sub(r'\s+', ' ', m.group(0))[:170])
