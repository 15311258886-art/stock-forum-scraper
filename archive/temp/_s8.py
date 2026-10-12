import os, re, sqlite3, json
B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
js = open(os.path.join(B, 'viewer', 'static', 'app.js'), encoding='utf-8').read()
ls = js.splitlines()

print('=== 1) NAV 完整结构（dashboard 落在哪）===')
i = js.find('const NAV')
j = js.find('];', i)
seg = js[i:j+2]
for n, l in enumerate(seg.splitlines(), 1):
    print('   %s' % l[:150])

print()
print('=== 2) 默认 tab / 初始渲染 ===')
for n, l in enumerate(ls, 1):
    if re.search(r'let (curTab|activeTab)|curTab\s*=|DEFAULT_TAB|location\.hash|selectNav\(', l):
        print('  %5d: %s' % (n, l.strip()[:150]))

print()
print('=== 3) 数据库时间口径核查 ===')
con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row
print('  trades 非空 post_time 最大值行:')
for r in con.execute("SELECT id, site, author, symbol, symbol_name, action, post_time FROM trades WHERE post_time IS NOT NULL AND TRIM(post_time)<>'' ORDER BY post_time DESC LIMIT 5"):
    print('    ', dict(r))
print()
print('  trades 按站点统计（非空）:')
for r in con.execute("SELECT site, COUNT(*) n, MIN(post_time) mn, MAX(post_time) mx FROM trades WHERE post_time IS NOT NULL AND TRIM(post_time)<>'' GROUP BY site"):
    print('    %-10s n=%-4s %s ~ %s' % (r['site'], r['n'], r['mn'], r['mx']))
print()
print('  stock_holders end_date 分布（Top6）:')
for r in con.execute('SELECT end_date, COUNT(*) n, COUNT(DISTINCT symbol) s FROM stock_holders GROUP BY end_date ORDER BY end_date DESC LIMIT 6'):
    print('    %s  n=%-6s 股=%s' % (r['end_date'], r['n'], r['s']))
print()
print('  2026-09-23 那批是哪些股（判断是否临时公告）:')
for r in con.execute("SELECT DISTINCT symbol, symbol_name FROM stock_holders WHERE end_date='2026-09-23' LIMIT 8"):
    print('    %s %s' % (r['symbol'], r['symbol_name']))
print()
print('  stock_holder_num end_date 分布:')
for r in con.execute('SELECT end_date, COUNT(*) n FROM stock_holder_num GROUP BY end_date ORDER BY end_date DESC LIMIT 5'):
    print('    %s  n=%s' % (r['end_date'], r['n']))
con.close()
