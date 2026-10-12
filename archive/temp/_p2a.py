import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
js = open('viewer/static/app.js', encoding='utf-8').read()
srv = open('viewer/server.py', encoding='utf-8').read()
ls = js.splitlines()

print('=== A) NAV 学习组完整定义 ===')
i = js.find('const NAV')
j = js.find('\n];', i)
seg = js[i:j+3]
# 只打印学习组
k = seg.find('key: "learn"')
print(seg[max(0, k-200): k+700] if k > 0 else seg[-1200:])

print()
print('=== B) renderConsensus 全文 ===')
i = js.find('function renderConsensus')
m = re.search(r'\nfunction [a-zA-Z]', js[i+10:])
j = i+10+m.start()+1 if m else i+4000
print(js[i:j])

print()
print('=== C) loadConsensus / api/consensus 调用 ===')
for n, l in enumerate(ls, 1):
    if re.search(r'loadConsensus|/api/consensus', l):
        print('%5d: %s' % (n, l.strip()[:170]))

print()
print('=== D) api_consensus 后端实现 ===')
i = srv.find('def api_consensus')
m = re.search(r'\ndef ', srv[i+10:])
j = i+10+m.start()+1 if m else i+3000
print(srv[i:j])

print()
print('=== E) 按需渲染分支（learn / consensus） ===')
for n, l in enumerate(ls, 1):
    if re.search(r'if \(tab === "(learn|consensus|cohold)",|tab\.startsWith\("learn', l):
        print('%5d: %s' % (n, l.strip()[:170]))
