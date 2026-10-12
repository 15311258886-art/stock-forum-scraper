# -*- coding: utf-8 -*-
"""读 api_consensus 实现 + renderConsensus 实现"""
import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = os.path.dirname(os.path.abspath(__file__))
os.chdir(B)

s = open('viewer/server.py', encoding='utf-8').read()
i = s.find('def api_consensus')
j = s.find('\ndef ', i + 10)
print('===== api_consensus (span %d-%d) =====' % (i, j))
print(s[i:j])
print()
print('===== 路由 /api/consensus =====')
for m in re.finditer(r'route == "/api/consensus[^"]*"', s):
    k = m.start()
    print(s[max(0, k - 150):k + 350])
    print('---')
