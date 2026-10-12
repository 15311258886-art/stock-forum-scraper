# -*- coding: utf-8 -*-
"""看清雪球/淘股吧人主页里「持仓线索」的确切结构（只读已存原始页）"""
import os, re, html

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'

def show(path, tag):
    if not os.path.exists(path):
        print('MISSING', path); return
    t = open(path, encoding='utf-8', errors='ignore').read()
    print('=' * 78)
    print(tag, os.path.basename(path), len(t), 'bytes')
    print('=' * 78)

    # 1) 沙盘：把含 持仓/建仓/买入/成本 的片段全打出来（去标签）
    for kw in ['建仓', '买入', '清仓', '成本', '盈亏', '仓位']:
        idxs = [m.start() for m in re.finditer(kw, t)]
        if not idxs:
            print('  [%s] 0 处' % kw); continue
        print('  ---- %s (%d 处) ----' % (kw, len(idxs)))
        for i in idxs[:3]:
            seg = t[max(0, i - 420):i + 320]
            seg = re.sub(r'<script.*?</script>', ' ', seg, flags=re.S)
            seg = re.sub(r'<style.*?</style>', ' ', seg, flags=re.S)
            seg = re.sub(r'<[^>]+>', ' ', seg)
            seg = html.unescape(seg)
            seg = re.sub(r'\s+', ' ', seg).strip()
            print('     ·', seg[:340])
    print()

    # 2) 时间格式
    print('  ---- 页面时间文本样例 ----')
    for pat, label in [(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}', 'YYYY-MM-DD HH:MM'),
                       (r'\d{1,2}-\d{1,2}\s+\d{2}:\d{2}', 'MM-DD HH:MM'),
                       (r'\d+分钟前', 'N分钟前'),
                       (r'\d+小时前', 'N小时前'),
                       (r'今天\s*\d{2}:\d{2}', '今天HH:MM'),
                       (r'\d{1,2}月\d{1,2}日', 'M月D日')]:
        h = re.findall(pat, t)
        print('    %-16s %3d 个  %s' % (label, len(h), h[:4]))
    print()

show(os.path.join(B, '_ax_xueqiu.html'), '雪球人主页')
show(os.path.join(B, '_ax_taoguba.html'), '淘股吧人主页')
