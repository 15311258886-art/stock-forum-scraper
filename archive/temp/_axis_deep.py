# -*- coding: utf-8 -*-
"""深挖：人主页里股票代码的真实写法 + 代码与日期的邻接关系。
决定能否从"人主页"直接抽出 (人, 代码, 时间, 动作)。"""
import os, re, html, json

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'

def load(fn):
    p = os.path.join(B, fn)
    if not os.path.exists(p):
        return None
    return open(p, encoding='utf-8', errors='ignore').read()

PATTERNS = [
    (r'SH\d{6}', 'SH前缀'),
    (r'SZ\d{6}', 'SZ前缀'),
    (r'\bsh\d{6}\b', '小写sh'),
    (r'\bsz\d{6}\b', '小写sz'),
    (r'\$[^$<>\s]{2,12}\$', '$符号$'),
    (r'\b6[0-9]{5}\b', '6开头6位'),
    (r'\b0[0-9]{5}\b', '0开头6位'),
    (r'\b3[0-9]{5}\b', '3开头6位'),
    (r'\b8[0-9]{5}\b', '8开头6位'),
    (r'\b4[0-9]{5}\b', '4开头6位'),
    (r'/S/(?:SH|SZ)?\d{6}', '/S/路径'),
    (r'stock[s]?/\d{6}', 'stock路径'),
    (r'quote[^"\']{0,20}\d{6}', 'quote路径'),
    (r'"symbol"\s*:\s*"[^"]+"', 'JSON symbol'),
    (r'"code"\s*:\s*"[^"]+"', 'JSON code'),
    (r'"stockCode"\s*:\s*"[^"]+"', 'JSON stockCode'),
]

for fn, tag in [('_ax_taoguba.html', '淘股吧-柯基吃柠檬'),
                ('_ax_xueqiu.html', '雪球-我没有行医资格证'),
                ('_ax_sina.html', '新浪-新浪财经客')]:
    t = load(fn)
    print('=' * 78)
    print('[%s]  %s  bytes=%s' % (tag, fn, len(t) if t else 'MISSING'))
    if not t:
        continue
    print('-' * 78)
    for pat, name in PATTERNS:
        hits = re.findall(pat, t)
        if hits:
            uniq = list(dict.fromkeys(hits))[:8]
            print('  %-16s %4d 个  样例: %s' % (name, len(hits), uniq))
    print()
    # 抽带股票代码的 a 标签
    anchors = re.findall(r'<a[^>]{0,200}?(?:href|data-href|data-url)=["\']([^"\']{6,120})["\'][^>]*>(.{0,30}?)</a>', t, re.S)
    stock_anchors = [a for a in anchors if re.search(r'(?:SH|SZ|sh|sz)?\d{6}|/S/|quote|stock', a[0] + a[1])]
    print('  含代码特征的长链接数:', len(stock_anchors))
    for h, txt in stock_anchors[:6]:
        print('     %-58s | %s' % (h[:58], re.sub(r'<[^>]+>', '', txt)[:20]))
    print()
    # 日期与代码的邻近（打印含代码的文本片段）
    ctx = []
    for m in re.finditer(r'(?:SH|SZ|sh|sz)\d{6}|/S/(?:SH|SZ)?\d{6}', t):
        i = m.start()
        seg = t[max(0, i - 220):i + 120]
        seg = re.sub(r'<[^>]+>', ' ', seg)
        seg = re.sub(r'\s+', ' ', seg).strip()
        ctx.append(seg[-260:])
    print('  代码上下文（前 4 条）:')
    for c in list(dict.fromkeys(ctx))[:4]:
        print('     ·', c[:250])
    print()
