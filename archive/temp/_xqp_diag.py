# -*- coding: utf-8 -*-
"""诊断：雪球人主页里到底有没有我们要的持仓线索，格式长什么样。"""
import os, re, json

B = os.path.dirname(os.path.abspath(__file__))
CD = os.path.join(B, '_xq_person_pages')

out = []
files = sorted(os.listdir(CD)) if os.path.isdir(CD) else []
out.append('缓存页: %s' % files)

for fn in files[:2]:
    p = os.path.join(CD, fn)
    raw = open(p, encoding='utf-8', errors='ignore').read()
    out.append('')
    out.append('=' * 70)
    out.append('文件 %s  %d 字节' % (fn, len(raw)))
    # 1) $名称(代码)$ 标准格式
    m1 = re.findall(r'\$([^$\n]{1,24}?)\((\d{6}|[A-Z]{2}\d{6})\)\$', raw)
    out.append('  $名称(代码)$ 命中: %d  %s' % (len(m1), m1[:8]))
    # 2) timeline 条目
    out.append('  timeline__item 出现: %d' % raw.count('timeline__item'))
    out.append('  status__item 出现: %d' % raw.count('status__item'))
    out.append('  article__ 出现: %d' % raw.count('article__'))
    # 3) 日期
    dates = re.findall(r'20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}', raw)
    out.append('  日期时间(YYYY-MM-DD HH:MM): %d  %s' % (len(dates), dates[:6]))
    d2 = re.findall(r'20\d{2}年\d{1,2}月\d{1,2}日', raw)
    out.append('  中文日期: %d  %s' % (len(d2), d2[:6]))
    # 4) 动作词
    for kw in ['清仓', '建仓', '买入', '卖出', '加仓', '减仓', '持仓', '成本', '抢权', '除息', '投资日志']:
        n = raw.count(kw)
        if n:
            out.append('    动作词 %s: %d' % (kw, n))
    # 5) 找一段含 建仓/清仓 的上下文
    for kw in ['清仓', '建仓', '投资日志']:
        i = raw.find(kw)
        if i > 0:
            seg = re.sub(r'<[^>]+>', ' ', raw[max(0, i-600):i+400])
            seg = re.sub(r'\s+', ' ', seg)
            out.append('    [%s] 上下文: %s' % (kw, seg[:420]))
            break

open(os.path.join(B, '_xqp_diag_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out))
