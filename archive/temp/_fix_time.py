# -*- coding: utf-8 -*-
"""时间字段诚实化修复（写库；执行前已备份 app.db.bak.before-timefix-*）

原则：能证实的保留/恢复；不能证实的一律置空（前端显示"未披露"），
      绝不用抓取时刻冒充交易/发帖时间。
"""
import os, sys, sqlite3, time
from collections import Counter

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(BASE, 'viewer', 'app.db')
sys.path.insert(0, BASE)

con = sqlite3.connect(DB, timeout=40)
cur = con.cursor()


def n(sql, args=()):
    return cur.execute(sql, args).fetchone()[0]


print('=' * 70)
print('修复前')
print('=' * 70)
for t, col in (('news', 'publish_time'), ('trades', 'post_time'), ('positions', 'post_time')):
    tot = n(f'SELECT COUNT(*) FROM {t}')
    empty = n(f"SELECT COUNT(*) FROM {t} WHERE {col} IS NULL OR TRIM({col})=''")
    print(f'  {t:<10} 共 {tot:<5} 空 {empty}')

# ---------------------------------------------------------------- 1) 新浪重放恢复
print()
print('=' * 70)
print('[1] 新浪：从 raw_pages 离线重放列表页，恢复真实时间')
print('=' * 70)
pages = [dict(zip(('site', 'url', 'html'), r)) for r in cur.execute(
    "SELECT site, url, html FROM raw_pages WHERE site='sina'")]

sys.path.insert(0, os.path.join(BASE, 'scraper'))
from scraper.parsers.sina import SinaParser  # noqa: E402

parser = SinaParser({'base_url': 'https://guba.sina.com.cn'})
url_time = {}      # 帖子 URL → 'YYYY-MM-DD HH:MM:SS'（UTC）
for p in pages:
    if 's=bar' not in p['url']:
        continue
    try:
        for it in parser.parse_news(p['html'], p['url']):
            if it.url and it.publish_time:
                url_time[it.url] = it.publish_time.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        print(f'  重放异常 {p["url"][:60]}: {type(e).__name__} {e}')
print(f'  重放得到 {len(url_time)} 条真实时间')

# news
rec_n = 0
for rowid, url in cur.execute("SELECT id, url FROM news WHERE site='sina'").fetchall():
    real = url_time.get(url)
    if real:
        cur.execute('UPDATE news SET publish_time=? WHERE id=?', (real, rowid))
        rec_n += 1
print(f'  news(sina) 恢复 {rec_n} 条真实时间')

# trades
rec_t = 0
for rowid, url in cur.execute("SELECT id, post_url FROM trades WHERE site='sina'").fetchall():
    real = url_time.get(url)
    if real:
        cur.execute('UPDATE trades SET post_time=? WHERE id=?', (real, rowid))
        rec_t += 1
print(f'  trades(sina) 恢复 {rec_t} 条真实时间')

# ---------------------------------------------------------------- 2) 识别并置空不可证实的时间
print()
print('=' * 70)
print('[2] 识别并置空"不可证实"的时间')
print('=' * 70)


def blank_unprovable(table, col, sites, label):
    """判定"不可证实"的时间并置空。

    判据（任一命中即为抓取时刻兜底，不是真实发生时间）：
      a) 与本站 raw_pages 的抓取分钟相同；
      b) 同秒钟 ≥3 条完全相同（批量写入特征）；
      c) 同分钟 ≥20 条（批量写入特征）。
    注：雪球解析器本身"解析失败即留空"，不兜底，故不在此列。
    """
    total = 0
    for site in sites:
        fetch_minutes = {r[0][:16] for r in cur.execute(
            "SELECT fetched_at FROM raw_pages WHERE site=?", (site,))}
        rows = cur.execute(
            f"SELECT id, {col} FROM {table} WHERE site=? AND {col} IS NOT NULL AND TRIM({col})<>''",
            (site,)).fetchall()
        if not rows:
            continue
        sec_c = Counter(v[:19] for _, v in rows)
        min_c = Counter(v[:16] for _, v in rows)
        bad = [rid for rid, v in rows
               if sec_c[v[:19]] >= 3 or min_c[v[:16]] >= 20 or v[:16] in fetch_minutes]
        if bad:
            cur.executemany(f"UPDATE {table} SET {col}='' WHERE id=?",
                            [(i,) for i in bad])
            total += len(bad)
        peak = min_c.most_common(1)[0][1] if min_c else 0
        print(f'  {label} {site:<10} 置空 {len(bad)}/{len(rows)} 条'
              f'  (同分钟最多 {peak} 条, 抓取窗口 {len(fetch_minutes)} 分钟)')
    return total


tn = blank_unprovable('news', 'publish_time', ['eastmoney', 'ths', 'taoguba', 'sina'], 'news')
tt = blank_unprovable('trades', 'post_time', ['eastmoney', 'ths', 'taoguba', 'sina'], 'trades')

# taoguba 持仓：博主页无日期，时间不可证实 → 置空（jisilu 已核为 2021-06-20，保留）
cur.execute("UPDATE positions SET post_time='' WHERE site='taoguba'")
tg = cur.rowcount
print(f'  positions taoguba 置空 {tg} 条（源页无日期）')

con.commit()

# ---------------------------------------------------------------- 3) 修复后
print()
print('=' * 70)
print('修复后')
print('=' * 70)
for t, col in (('news', 'publish_time'), ('trades', 'post_time'), ('positions', 'post_time')):
    tot = n(f'SELECT COUNT(*) FROM {t}')
    empty = n(f"SELECT COUNT(*) FROM {t} WHERE {col} IS NULL OR TRIM({col})=''")
    print(f'  {t:<10} 共 {tot:<5} 空 {empty}  → 有时间 {tot - empty}')

print()
print('  剩余有时间的 trades（应全部为可证实的真实值）：')
for r in cur.execute("SELECT site, post_time, action, symbol_name FROM trades "
                     "WHERE post_time IS NOT NULL AND TRIM(post_time)<>'' ORDER BY site, post_time"):
    print('    ', r)
print()
print('  剩余有时间的 positions：')
for r in cur.execute("SELECT site, MIN(post_time) mn, MAX(post_time) mx, COUNT(*) c FROM positions "
                     "WHERE post_time IS NOT NULL AND TRIM(post_time)<>'' GROUP BY site"):
    print('    ', r)
print()
print('  完整性:', cur.execute('PRAGMA integrity_check').fetchone()[0])
con.close()
print('=' * 70)
print('DONE')
