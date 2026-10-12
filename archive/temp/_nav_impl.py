# -*- coding: utf-8 -*-
"""导出子导航渲染逻辑与待整合面板实现（只读）"""
import os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
ls = js.splitlines()

out = []

def dump(fn_name, limit=140):
    i = js.find('function ' + fn_name)
    if i < 0:
        out.append('## %s NOT FOUND' % fn_name)
        return
    # 找函数结束（下一个顶格 function 或 const）
    seg = js[i:i + 4000]
    out.append('## %s @ line %d' % (fn_name, js[:i].count('\n') + 1))
    out.append(seg[:limit])
    out.append('')

out.append('=== paintSubnav 全文 ===')
dump('paintSubnav', 3200)
out.append('=== renderNav 全文 ===')
dump('renderNav', 1800)
out.append('=== initNav 全文 ===')
dump('initNav', 2600)
out.append('=== renderPersons 全文 ===')
dump('renderPersons', 2200)
out.append('=== renderOperating 全文 ===')
dump('renderOperating', 2200)

open(os.path.join(B, '_nav_impl_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('written', len('\n'.join(out)))
