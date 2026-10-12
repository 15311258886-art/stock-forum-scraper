# -*- coding: utf-8 -*-
"""时间修复预案（只读）：判断哪些行的时间可从原始页重放恢复，哪些必须置空。"""
import os, sys, re, sqlite3, io
from collections import Counter

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(BASE, 'viewer', 'app.db')
sys.path.insert(0, BASE)

con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
con.row_factory = sqlite3.Row

# ---------- 1) 各站 raw_pages ----------
print('=== raw_pages 概览 ===')
pages = [dict(r) for r in con.execute(
    'SELECT site, url, html, fetched_at FROM raw_pages')]
print(f'  总 {len(pages)} 页')
by_site = Counter(p['site'] for p in pages)
print('  ', dict(by_site))

# ---------- 2) 各站 news/trades url 命中率 ----------
def hit_rate(site, table, urlcol):
    rows = [r[urlcol] for r in con.execute(
        f'SELECT {urlcol} FROM {table} WHERE site=?', (site,))]
    urls = [u for u in rows if u]
    if not urls:
        return 0, 0
    page_urls = {p['url'] for p in pages if p['site'] == site}
    hit = sum(1 for u in urls if u in page_urls)
    return hit, len(urls)

print()
print('=== 明细页是否在 raw_pages（决定能否重放恢复）===')
for site in ('sina', 'taoguba', 'eastmoney', 'ths'):
    for table, col in (('news', 'url'), ('trades', 'post_url')):
        h, n = hit_rate(site, table, col)
        print(f'  {site:<10} {table:<7} {col:<9} 命中 {h}/{n}')

# ---------- 3) 用修好的 sina 解析器离线重放 ----------
print()
print('=== 新浪：离线重放 6 个列表页 → 真实发布时间 ===')
sys.path.insert(0, os.path.join(BASE, 'scraper'))
try:
    from scraper.parsers import sina as sina_parser
except Exception:
    from parsers import sina as sina_parser  # type: ignore

recovered = {}   # url -> 'YYYY-MM-DD HH:MM:SS'
for p in pages:
    if p['site'] != 'sina' or 's=bar' not in p['url']:
        continue
    try:
        items = sina_parser.parse_list(p['html'], p['url'])
    except Exception as e:
        print(f'  重放失败 {p["url"][:60]}: {type(e).__name__} {e}')
        continue
    for it in (items or []):
        t = it.get('time') or it.get('post_time')
        u = it.get('url')
        if u and t:
            recovered[u] = str(t)[:19]

print(f'  重放得到 {len(recovered)} 条 (url → 时间)')
for u, t in list(recovered.items())[:6]:
    print(f'    {t}  {u[:74]}')

# 覆盖率：news 表里 sina 的 url 有多少能对上
sina_news = [dict(r) for r in con.execute(
    "SELECT id, url, publish_time FROM news WHERE site='sina'")]
cover = [r for r in sina_news if r['url'] in recovered]
print(f'  news(sina) 共 {len(sina_news)} 条 → 可恢复 {len(cover)} 条')

# ---------- 4) 抓取窗口（用于识别伪时间） ----------
print()
print('=== 各站抓取窗口（fetched_at）===')
for site in ('sina', 'taoguba', 'eastmoney', 'ths', 'xueqiu'):
    ts = sorted({p['fetched_at'][:16] for p in pages if p['site'] == site})
    print(f'  {site:<10} {len(ts)} 个不同分钟: {ts[:5]}{" ..." if len(ts) > 5 else ""}')

# ---------- 5) 现有时间分布（判断伪时间） ----------
print()
print('=== news 时间：出现次数最多的 8 个分钟 ===')
for site in ('sina', 'taoguba', 'eastmoney', 'ths', 'xueqiu'):
    c = Counter(r['publish_time'][:16] for r in con.execute(
        "SELECT publish_time FROM news WHERE site=?", (site,)))
    top = c.most_common(3)
    print(f'  {site:<10} n={sum(c.values()):<4} 最高频: {top}')

print()
print('=== trades 时间：非空明细按站点 ===')
for site in ('sina', 'taoguba', 'eastmoney', 'ths', 'xueqiu'):
    rows = [dict(r) for r in con.execute(
        "SELECT id, author, post_time, action, symbol_name, post_url FROM trades WHERE site=?", (site,))]
    ne = [r for r in rows if (r['post_time'] or '').strip()]
    print(f'  {site:<10} n={len(rows):<4} 非空={len(ne)}')
    for r in ne[:4]:
        print(f"      id={r['id']} {r['post_time']} {r['action']} {r['symbol_name']} | {(r['post_url'] or '')[:56]}")

con.close()
