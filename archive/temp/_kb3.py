import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B,'viewer','static','app.js'), encoding='utf-8').read()
h  = open(os.path.join(B,'viewer','static','index.html'), encoding='utf-8').read()

print('app.js lines', len(js.splitlines()))
print()
print('=== NAV 定义 ===')
i = js.find('const NAV'); j = js.find('\n];', i)
print(js[i:j+3] if i>=0 else 'NOT FOUND')

print()
print('=== 学习板块相关函数 ===')
for n,l in enumerate(js.splitlines(),1):
    if re.search(r'learn|Learn|学习|zdx|zdh|基础知识|track', l):
        print('%5d: %s' % (n, l.rstrip()[:175]))

print()
print('=== index.html panels ===')
print([m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)])

print()
print('=== 共识面板相关 ===')
for n,l in enumerate(js.splitlines(),1):
    if re.search(r'renderConsensus|loadConsensus|p-consensus|cohold|sells', l):
        print('%5d: %s' % (n, l.rstrip()[:175]))
