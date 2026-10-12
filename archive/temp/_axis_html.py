# -*- coding: utf-8 -*-
"""抽取雪球 timeline item 原始 HTML 结构（含 cashtag 的那条），作为解析器依据"""
import os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
t = open(os.path.join(B, '_ax_xueqiu.html'), encoding='utf-8', errors='ignore').read()

# 找所有 timeline item 的边界
print('=== timeline item 相关 class ===')
for c in sorted(set(re.findall(r'class="(timeline__[a-z_\- ]+)"', t))):
    print('  ', c)
print()
print('=== status 容器 class ===')
for c in sorted(set(re.findall(r'class="([^"]*status[^"]*)"', t)))[:20]:
    print('  ', c)
print()

# 抽出含 cashtag 的第一个 item（原始 HTML，限长）
m = re.search(r'\$[^$<]{2,20}\(S[ZH]\d{6}\)\$', t)
if m:
    i = m.start()
    # 往前找容器起点
    start = t.rfind('timeline__item', max(0, i - 6000), i)
    seg = t[start if start > 0 else max(0, i - 3000): i + 1200]
    print('=== cashtag 所在片段原始 HTML（%d 字符）===' % len(seg))
    print(seg[:4200])
