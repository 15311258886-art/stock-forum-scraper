# -*- coding: utf-8 -*-
"""探源2：① 雪球列表页时间格式（为何部分解析失败）② 东财原帖可否用浏览器取时间"""
import os, re, sqlite3, json

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=' * 70)
print('A) 雪球列表页里的时间格式（对照 PID 已知/未知）')
print('=' * 70)
# 取 000001 的雪球搜索页
pg = con.execute("SELECT url, html FROM raw_pages WHERE site='xueqiu' AND url LIKE '%k?q=000001%' LIMIT 1").fetchone()
html = pg['html'] if pg else ''
print('页长度', len(html))

# 目标 PID：410379180（已取到时间）、410294815（已取到）、410586958（未取到）
for pid in ['410379180', '410294815', '410586958']:
    print()
    print('--- PID', pid, '---')
    idxs = [m.start() for m in re.finditer(re.escape(pid), html)]
    print('   出现', len(idxs), '次')
    for i in idxs[:2]:
        seg = html[max(0, i - 500):i + 200]
        seg = re.sub(r'\s+', ' ', seg)
        print('   >', seg[-620:])
    # 全页时间样式统计
print()
print('=== 页面内时间样式统计 ===')
for pat, name in [
    (r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?', 'YYYY-MM-DD HH:MM'),
    (r'\d{1,2}-\d{1,2}\s+\d{2}:\d{2}', 'MM-DD HH:MM'),
    (r'\d{1,2}月\d{1,2}日\s*\d{2}:\d{2}', 'M月D日 HH:MM'),
    (r'\d{1,2}小时前|\d{1,3}分钟前|刚刚', '相对时间'),
    (r'"created_at"\s*:\s*\d+', 'created_at(ms)'),
    (r'"timeBefore"\s*:\s*"([^"]*)"', 'timeBefore'),
    (r'"created_at"\s*:\s*"?([\d\-: ]+)"?', 'created_at字符串'),
]:
    hits = re.findall(pat, html)
    print('  %-22s %4d 个   前3: %s' % (name, len(hits), hits[:3]))

print()
print('=== 页面内 JSON 字段名（含时间语义）===')
for k in sorted(set(re.findall(r'"(\w*(?:time|Time|date|Date|created|Created)\w*)"', html))):
    print('  ', k)

con.close()
