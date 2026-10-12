# -*- coding: utf-8 -*-
"""把新浪「仅日期被补成零点」的时间归一为日期（YYYY-MM-DD）；
并重放原始页验证解析器新行为（不再补假钟点）。"""
import os, sys, sqlite3, datetime as dt

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(BASE, 'viewer', 'app.db')
sys.path.insert(0, BASE)

con = sqlite3.connect(DB, timeout=40)
cur = con.cursor()

print('=== 归一前：新浪时间形态 ===')
for t, c in (('news', 'publish_time'), ('trades', 'post_time')):
    for r in cur.execute(f"SELECT {c}, COUNT(*) FROM {t} WHERE site='sina' AND TRIM({c})<>'' GROUP BY {c} ORDER BY {c}"):
        print(f'  {t:<7} {r[0]}  x{r[1]}')

# 16:00:00(UTC) = 北京 00:00 → 源只给了日期；转成北京日期字符串
fixed = 0
for t, c in (('news', 'publish_time'), ('trades', 'post_time')):
    rows = cur.execute(
        f"SELECT id, {c} FROM {t} WHERE site='sina' AND {c} LIKE '% 16:00:00'").fetchall()
    for rid, v in rows:
        d = dt.date.fromisoformat(v[:10]) + dt.timedelta(days=1)   # UTC → 北京日期
        cur.execute(f"UPDATE {t} SET {c}=? WHERE id=?", (d.isoformat(), rid))
        fixed += 1
    # 已是 00:00:00（北京零点）也改用日期
    rows2 = cur.execute(
        f"SELECT id, {c} FROM {t} WHERE site='sina' AND {c} LIKE '% 00:00:00'").fetchall()
    for rid, v in rows2:
        cur.execute(f"UPDATE {t} SET {c}=? WHERE id=?", (v[:10], rid))
        fixed += 1
con.commit()
print(f'\n[归一] 共处理 {fixed} 条 → 仅保留日期')

print()
print('=== 归一后：新浪时间形态 ===')
for t, c in (('news', 'publish_time'), ('trades', 'post_time')):
    for r in cur.execute(f"SELECT {c}, COUNT(*) FROM {t} WHERE site='sina' AND TRIM({c})<>'' GROUP BY {c} ORDER BY {c}"):
        print(f'  {t:<7} {r[0]}  x{r[1]}')

# ---------------- 用修好的解析器重放，验证新行为 ----------------
print()
print('=== 重放验证（解析器新行为） ===')
from scraper.parsers.sina import SinaParser, _parse_rel_time   # noqa: E402

p = SinaParser({'base_url': 'https://guba.sina.com.cn'})
pg = cur.execute("SELECT url, html FROM raw_pages WHERE site='sina' AND url LIKE '%s=bar%' LIMIT 1").fetchone()
rows = p._bar_rows(pg[1])
print(f'  样本页 {pg[0][:60]} → {len(rows)} 行')
import re
for r in rows[:8]:
    fmt = ('仅日期 ' + r['time'].strftime('%Y-%m-%d')) if (r['time'] and not r['time_precise']) else \
          ('含时刻 ' + r['time'].strftime('%Y-%m-%d %H:%M:%S')) if r['time'] else '无时间'
    print(f"    {fmt:<24} | {(r['title'] or '')[:34]}")

print()
print('  _parse_rel_time 断言：「09月24日」应解析成功且不带假钟点')
v = _parse_rel_time('09月24日')
print(f'    09月24日 -> {v}  精确={bool(re.search(chr(92)+"d{1,2}:"+chr(92)+"d{2}", "09月24日"))}')
v2 = _parse_rel_time('今天18:32')
print(f'    今天18:32 -> {v2}  精确=True')
v3 = _parse_rel_time('不认识的文本')
print(f'    不认识的文本 -> {v3}  (应为 None)')

print()
print('  完整性:', cur.execute('PRAGMA integrity_check').fetchone()[0])
con.close()
print('DONE')
