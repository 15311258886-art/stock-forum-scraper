import sqlite3, os

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=== 各表列（含 axis/stale 情况）===')
for t in ['players', 'positions', 'trades', 'posts', 'news']:
    cols = [r[1] for r in con.execute('PRAGMA table_info(%s)' % t)]
    print('  %-10s axis=%s stale=%s' % (t, 'axis' in cols, 'stale' in cols))
    print('     ', cols)

print()
print('=== quality_filter.py 打了哪些表 ===')
s = open(os.path.join(B, 'quality_filter.py'), encoding='utf-8').read()
import re
for m in re.finditer(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", s):
    print('  ', m.group(1), '->', m.group(2))
print()
for m in re.finditer(r"TABLES\s*=\s*\[([^\]]+)\]", s):
    print('  TABLES =', m.group(1))
for m in re.finditer(r"TABLE\w*\s*=\s*\[([^\]]+)\]", s):
    print('  TABLE =', m.group(1))

print()
print('=== quality_filter 实际打标的表/字段 ===')
for i, l in enumerate(s.splitlines(), 1):
    if re.search(r'ALTER TABLE|UPDATE |axis|stale|TABLES|for t in', l):
        print('%4d: %s' % (i, l.strip()[:150]))

con.close()
