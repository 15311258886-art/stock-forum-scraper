
# -*- coding: utf-8 -*-
"""定位持仓 chip 渲染 + api_players hold 组装"""
import os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
ls = js.splitlines()

print('app.js lines', len(ls))
print()
print('=== 关键词定位 ===')
for i, l in enumerate(ls, 1):
    if re.search(r'hold-chip|chip-time|hlShown|const hls|holdTimeTag|showAllHolds|holdTime|关注/操作', l):
        print('%5d: %s' % (i, l.rstrip()[:190]))

print()
print('=== 附近区间 1960-2025 ===')
for i in range(1959, min(2026, len(ls))):
    print('%5d: %s' % (i + 1, ls[i][:190]))
