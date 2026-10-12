# -*- coding: utf-8 -*-
"""导出 NAV 全结构 + 每个子标签的数据源，用于归类重组决策。"""
import os, re, io, json

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()

out = []
out.append('=== NAV 全结构 ===')
i = js.find('const NAV')
j = js.find('\n];', i)
seg = js[i:j + 3]
out.append(seg)
out.append('')

out.append('=== 各 tab 的渲染函数与接口调用 ===')
# 找 selectNav / 按需渲染段
for m in re.finditer(r'if \(tab === "([a-z_]+)"\)\s*\{([^}]{0,400})', js):
    body = re.sub(r'\s+', ' ', m.group(2))[:200]
    out.append('  tab=%-14s -> %s' % (m.group(1), body))
out.append('')

out.append('=== 每个 tab 用到的 /api/ 接口 ===')
for m in re.finditer(r'(?:function|const)\s+(render|load)([A-Z]\w*)\b', js):
    pass
# 直接扫 load* 函数体里的 api 调用
for fn in re.finditer(r'function (load[A-Z]\w*)\([^)]*\)\s*\{', js):
    name = fn.group(1)
    body = js[fn.end(): fn.end() + 600]
    apis = re.findall(r'api\(\s*["\'`](/api/[a-z_/?=&{}$A-Za-z0-9_.-]*)', body)
    if apis:
        out.append('  %-22s -> %s' % (name, ', '.join(sorted(set(apis)))))
out.append('')

out.append('=== 各 panel id（index.html 静态面板）===')
h = open(os.path.join(B, 'viewer', 'static', 'index.html'), encoding='utf-8').read()
ids = [m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)]
out.append('  ' + ', '.join(ids))
out.append('')

out.append('=== APP 一级分组 ===')
for m in re.finditer(r'\{\s*key:\s*"([a-z_]+)",\s*label:\s*"([^"]+)"', js):
    out.append('  key=%-14s label=%s' % (m.group(1), m.group(2)))

open(os.path.join(B, '_nav_g_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out))
