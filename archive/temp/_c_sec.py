"""核验 lhb_sector_link.py 的 T+1 缺失容忍改动 + 汇总统计 None 处理。"""
import os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
p = os.path.join(B, 'lhb_sector_link.py')
s = open(p, encoding='utf-8').read()
ls = s.splitlines()

print('TOTAL', len(ls))
print()
print('=== 关键标记 ===')
for k in ['pending_t1', '待验证', 'if not r1s', 'continue']:
    print('  %-14s %d' % (k, s.count(k)))
print()

print('=== 计算 + results.append 段（260-340）===')
for i in range(259, min(345, len(ls))):
    print('%5d: %s' % (i + 1, ls[i][:170]))
print()

print('=== summary 汇总段 ===')
i = s.find('summary')
while i > 0:
    seg = s[i:i + 60]
    if 't1' in s[i:i + 1200] and ('round' in s[i:i + 1200] or 'avg' in s[i:i + 1200]):
        print('@line', s[:i].count('\n') + 1)
        for j, l in enumerate(s[i - 200:i + 1600].splitlines(), 1):
            print('   %s' % l[:170])
        break
    i = s.find('summary', i + 1)
