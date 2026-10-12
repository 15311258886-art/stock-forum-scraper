import sys, re, os
sys.stdout.reconfigure(encoding='utf-8')
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
css = open(os.path.join(B, 'viewer', 'static', 'app.css'), encoding='utf-8').read()
out = []
out.append('app.css chars %d, lines %d' % (len(css), len(css.splitlines())))
out.append('ml- count: %d | lv- count: %d' % (css.count('ml-'), css.count('lv-')))
out.append('')
out.append('=== ml-/lv- 样式定义 ===')
for m in re.finditer(r'[^\n{}]*\.(ml-[a-z0-9-]+|lv-[a-z]+)[^{]*\{[^}]*\}', css):
    out.append('  ' + re.sub(r'\s+', ' ', m.group(0))[:220])
out.append('')
out.append('=== app.css 尾部 800 字符 ===')
out.append(css[-800:])

# 渲染代码用到的类
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
i = js.find('function renderDashboard')
j = js.find('function loadStarMoves')
seg = js[i:j] if j > i else js[i:i+6000]
used = sorted(set(re.findall(r'\b(ml-[a-z0-9-]+|lv-[a-z]+)\b', seg)))
defined = sorted(set(re.findall(r'\.(ml-[a-z0-9-]+|lv-[a-z]+)', css)))
out.append('')
out.append('渲染用到的: %s' % used)
out.append('CSS 已定义: %s' % defined)
out.append('缺失: %s' % [u for u in used if u not in defined])

open(os.path.join(B, '_v9_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out))
