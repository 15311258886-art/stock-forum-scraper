# -*- coding: utf-8 -*-
"""重放 raw_pages 里的 sina HTML，验证时间列到底取到什么。只读。"""
import sqlite3
import sys

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
sys.path.insert(0, BASE)
from bs4 import BeautifulSoup  # noqa: E402

DB = BASE + r'\viewer\app.db'
con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
con.row_factory = sqlite3.Row

rows = list(con.execute("SELECT id, url, fetched_at, html FROM raw_pages WHERE site='sina' ORDER BY id DESC LIMIT 3"))
print(f'sina raw_pages 取到 {len(rows)} 页\n')

for pg in rows:
    print('=' * 100)
    print(f"page id={pg['id']} fetched={pg['fetched_at']} url={pg['url']}")
    html = pg['html']
    print(f'html len={len(html)}')
    soup = BeautifulSoup(html, 'lxml')
    trs = []
    for a in soup.select('a[href*=tid]'):
        tr = a.find_parent('tr')
        if tr is None:
            continue
        trs.append((a, tr))
    print(f'含 tid 链接的 <tr> 行数: {len(trs)}')
    for a, tr in trs[:6]:
        tds = tr.find_all('td')
        texts = [t.get_text(' ', strip=True)[:26] for t in tds]
        print(f"  标题: {a.get_text(' ', strip=True)[:40]}")
        print(f"     td 数={len(tds)} 各 td 文本={texts}")
        print(f"     tds[-1] = {tds[-1].get_text(' ', strip=True)!r}")
        # 找所有 td 里像时间的
        for i, t in enumerate(tds):
            txt = t.get_text(' ', strip=True)
            if any(k in txt for k in ('今天', '昨天', '分钟前', '小时前')) or (len(txt) <= 12 and ':' in txt):
                print(f'       >> td[{i}] 像时间: {txt!r}')
    print()

con.close()
