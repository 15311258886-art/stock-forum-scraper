# -*- coding: utf-8 -*-
"""定位雪球 timeline item 的发布时间与动作词，作为人轴解析器依据"""
import os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
t = open(os.path.join(B, '_ax_xueqiu.html'), encoding='utf-8', errors='ignore').read()
print('html len', len(t))

# 1) 时间相关 class / 属性
print('=== time 相关 ===')
for c in sorted(set(re.findall(r'class="([^"]*time[^"]*)"', t)))[:20]:
    print('  class:', c)
for m in list(re.finditer(r'(?:datetime|data-time|data-created_at)="([^"]+)"', t))[:8]:
    print('  attr:', m.group(0)[:80])
for m in list(re.finditer(r'<a[^>]*class="[^"]*timeline__item__info[^"]*"[^>]*>(.{0,200}?)</a>', t, re.S))[:3]:
    print('  info-block:', re.sub(r'\s+', ' ', m.group(1))[:180])

# 2) timeline__item 完整切分（拿第 2 个样本）
print()
items = [m.start() for m in re.finditer(r'class="timeline__item"', t)]
print('timeline__item count:', len(items))
for k, i in enumerate(items[:2]):
    seg = t[i: i + 2600]
    print('--- item %d 时间候选 ---' % k)
    for m in re.finditer(r'(20\d{2}-\d{1,2}-\d{1,2}[ T]?\d{0,2}:?\d{0,2})|(\d{1,2}-\d{1,2}\s+\d{2}:\d{2})|(\d+\s*分钟前|\d+\s*小时前|今天\s*\d{2}:\d{2})', seg):
        print('   ', repr(m.group(0)))
    # 动作词
    acts = [w for w in ('建仓', '清仓', '加仓', '减仓', '买入', '卖出', '持仓', '成本', '抢权', '调仓') if w in seg]
    print('    动作词:', acts)
    print('    cashtag:', re.findall(r'\$([^$<]{2,22})\((S[ZH]\d{6})\)\$', seg)[:6])

# 3) 是否有 cashtag 短码形式（$名称$ 或 SH600519）
print()
print('=== 其它代码形式 ===')
print('  $名(SH600519)$:', len(re.findall(r'\$[^$<]{1,22}\(S[ZH]\d{6}\)\$', t)))
print('  (SH600519):', len(re.findall(r'\(S[ZH]\d{6}\)', t)))
print('  /S/SH600519:', len(re.findall(r'/S/S[ZH]\d{6}', t)))
