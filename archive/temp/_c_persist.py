"""读 lhb_sector_link.py 的 _persist 落库段。"""
import os
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
s = open(os.path.join(B, 'lhb_sector_link.py'), encoding='utf-8').read()
ls = s.splitlines()
print('TOTAL', len(ls))
i = s.find('_persist')
if i < 0:
    i = s.find('lhb_sector_focus')
print('_persist at char', i, '=> line', s[:i].count('\n') + 1)
print()
start = s[:i].count('\n')
for n in range(max(0, start - 2), min(len(ls), start + 70)):
    print('%4d: %s' % (n + 1, ls[n][:175]))
