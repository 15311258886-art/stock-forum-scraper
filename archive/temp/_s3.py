import re, os
BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(BASE, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
h = open(os.path.join(BASE, 'viewer', 'static', 'index.html'), encoding='utf-8').read()
out = []
out.append('=== index.html panel ids ===')
ids = [m.group(1) for m in re.finditer(r'id="(p-[a-z_]+)"', h)]
out.append('  ' + ', '.join(ids))
out.append('  count=%d | p-dashboard=%s' % (len(ids), 'p-dashboard' in ids))
out.append('')
out.append('=== 总览/汇总 附近面板 ===')
for m in re.finditer(r'<div class="panel"[^>]*id="p-(summary|players|lhb)"[^>]*></div>', h):
    out.append('  ' + m.group(0))
out.append('')
out.append('=== loadNiusan 定义 ===')
i = js.find('function loadNiusan')
out.append(js[i:i+900] if i > 0 else 'NOT FOUND')
out.append('')
out.append('=== const NIUSAN / LBHS 状态定义 ===')
for m in re.finditer(r'const (NIUSAN|LBHS|LHB)\s*=\s*\{[^}]*\};', js):
    out.append('  ' + m.group(0))
out.append('')
out.append('=== api() helper ===')
i = js.find('function api(')
out.append(js[i:i+500] if i > 0 else 'NOT FOUND')
out.append('')
out.append('=== loadAll 里的 fetch 模式（前两处） ===')
i = js.find('function loadAll')
seg = js[i:i+2600] if i > 0 else ''
out.append(seg[:2400])
open(os.path.join(BASE, '_s3_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('WROTE', len('\n'.join(out)))
