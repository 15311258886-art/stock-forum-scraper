# -*- coding: utf-8 -*-
"""导航归拢前审计：导出 NAV 全结构 + 每个面板的渲染函数与数据来源。"""
import os, re, json

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
html = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()

out = []

# 1) NAV 全结构
i = js.find('const NAV')
j = js.find('\n];', i)
seg = js[i:j+3]
out.append('===== NAV 结构 =====')
out.append(seg)
out.append('')

# 2) 一级分组 key/label
out.append('===== 一级分组 =====')
for m in re.finditer(r'\{\s*key:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', seg):
    out.append('  key=%-16s label=%s' % (m.group(1), m.group(2)))
out.append('')

# 3) 二级子标签
out.append('===== 二级子标签 =====')
for m in re.finditer(r'\{\s*tab:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', seg):
    out.append('  tab=%-16s label=%s' % (m.group(1), m.group(2)))
out.append('')

# 4) 面板 id（index.html）
out.append('===== index.html 面板 id =====')
ids = re.findall(r'id="(p-[a-z_]+)"', html)
out.append('  ' + ', '.join(ids))
out.append('')

# 5) 每个 tab 的渲染函数
out.append('===== tab -> 渲染/加载函数 =====')
for m in re.finditer(r'tab:\s*"([a-z_]+)"', seg):
    tab = m.group(1)
    fn_render = re.findall(r'function (?:render)?([A-Z]\w*)\s*\(', js)
    # 直接找 if (tab === "xxx") 分支
    branches = re.findall(r'if \(tab === "' + tab + r'"\)\s*\{([^}]*)\}', js)
    out.append('  [%s]' % tab)
    for b in branches[:3]:
        out.append('     ' + re.sub(r'\s+', ' ', b.strip())[:200])
out.append('')

# 6) p-xxx 在各渲染函数里的使用
out.append('===== 文档中 #p-xxx 用法 =====')
seen = {}
for i, l in enumerate(js.splitlines(), 1):
    for m in re.finditer(r'#(p-[a-z_]+)', l):
        seen.setdefault(m.group(1), []).append(i)
for k in sorted(seen):
    out.append('  %-18s %s' % (k, seen[k][:8]))
out.append('')

# 7) loadAll 全貌
out.append('===== loadAll =====')
k = js.find('function loadAll')
if k > 0:
    out.append(js[k:k+2200])
out.append('')

open(os.path.join(B, '_nav_audit2_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out)[:12000])
