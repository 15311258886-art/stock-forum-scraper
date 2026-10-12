# -*- coding: utf-8 -*-
"""验证时间解析修复：单元用例 + 用 raw_pages 真实 HTML 重放。只读。"""
import sqlite3
import sys

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
sys.path.insert(0, BASE)

import py_compile  # noqa: E402

for f in ('scraper/parsers/sina.py', 'scraper/parsers/taoguba.py', 'scraper/models.py'):
    py_compile.compile(BASE + '\\' + f.replace('/', '\\'), doraise=True)
print('PY_COMPILE_OK (sina / taoguba / models)\n')

from scraper.parsers.sina import SinaParser, _parse_rel_time  # noqa: E402

print('=== 单元用例 ===')
cases = ['今天18:32', '昨天20:31', '09月24日', '09月20日', '2026-09-20', '18:32',
         '12月31日', '3分钟前', '2小时前', '', '   ', '垃圾文本', '×']
for c in cases:
    print(f'  {c!r:<14} -> {_parse_rel_time(c)}')

print()
print('=== 用 raw_pages 真实 HTML 重放（修复后）===')
DB = BASE + r'\viewer\app.db'
con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
con.row_factory = sqlite3.Row
parser = SinaParser({'base_url': 'https://guba.sina.com.cn'})
pages = list(con.execute("SELECT id, url, fetched_at, html FROM raw_pages WHERE site='sina' ORDER BY id"))
print(f'raw_pages sina 共 {len(pages)} 页')
for pg in pages:
    print(f"\n  -- page {pg['id']} 抓取于 {pg['fetched_at']}  {pg['url'][-28:]}")
    trs = list(parser.parse_trades(pg['html'], pg['url']))
    print(f'     parse_trades 命中 {len(trs)} 条：')
    for t in trs[:6]:
        print(f"       {t.post_time}  {t.action} {t.symbol}  {t.author}  | {t.raw_text[:30]}")
    # 统计本页时间解析成功率
    rows = parser._bar_rows(pg['html'])
    ok = sum(1 for r in rows if r['time'])
    print(f"     列表行 {len(rows)} 条，时间解析成功 {ok} / 失败 {len(rows)-ok}")

con.close()
