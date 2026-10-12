# -*- coding: utf-8 -*-
import sqlite3, os, re

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

for t in ('posts', 'trades', 'news'):
    print('=== %s cols ===' % t)
    print(' ', [r[1] for r in con.execute('PRAGMA table_info(%s)' % t)])
print()

print('=== taoguba posts 样例 ===')
cols = [r[1] for r in con.execute('PRAGMA table_info(posts)')]
sel = ','.join(cols)
for r in con.execute("SELECT %s FROM posts WHERE site='taoguba' ORDER BY rowid DESC LIMIT 8" % sel):
    d = dict(r)
    print(' ', {k: (str(v)[:110] if v is not None else None) for k, v in d.items()})
con.close()
