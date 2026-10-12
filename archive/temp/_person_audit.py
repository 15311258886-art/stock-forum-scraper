# -*- coding: utf-8 -*-
"""回查所有站点的抓取线索口径：是「以人为轴」还是「以帖子为轴」。
只读，不改数据。"""
import sqlite3, os, json, re

B = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=' * 78)
print('一、players 表：各站点是否有「人」这一层，及人的标识是什么')
print('=' * 78)
for r in con.execute("""SELECT site, channel, COUNT(*) n,
   SUM(CASE WHEN source_url IS NULL OR TRIM(source_url)='' THEN 1 ELSE 0 END) no_url,
   SUM(CASE WHEN name LIKE '股友%' OR name LIKE '用户%' OR name LIKE '匿名%' THEN 1 ELSE 0 END) generic_name
   FROM players GROUP BY site, channel ORDER BY n DESC"""):
    print('  %-10s ch=%-4s n=%-5s 无主页=%-5s 泛化名=%-5s' % (
        r['site'], r['channel'], r['n'], r['no_url'], r['generic_name']))

print()
print('=' * 78)
print('二、positions / trades：每条记录是否挂到了「人」上，人的名字是否可信')
print('=' * 78)
for tbl, namecol in [('positions', 'player'), ('trades', 'author')]:
    print('--- %s ---' % tbl)
    for r in con.execute("""SELECT site, COUNT(*) n,
       SUM(CASE WHEN %s IS NULL OR TRIM(%s)='' THEN 1 ELSE 0 END) no_person,
       SUM(CASE WHEN %s LIKE '股友%%' OR %s LIKE '用户%%' OR %s LIKE '匿名%%' THEN 1 ELSE 0 END) generic,
       SUM(CASE WHEN post_url IS NULL OR TRIM(post_url)='' THEN 1 ELSE 0 END) no_url
       FROM %s GROUP BY site ORDER BY n DESC""" % (namecol, namecol, namecol, namecol, namecol, tbl)):
        print('  %-10s n=%-5s 无人=%-4s 泛化名=%-4s 无链接=%s' % (
            r['site'], r['n'], r['no_person'], r['generic'], r['no_url']))

print()
print('=' * 78)
print('三、逐个站点：抓取线索是「人」还是「帖子」——看 post_url 指向哪')
print('=' * 78)
for tbl, namecol in [('positions', 'player'), ('trades', 'author')]:
    for r in con.execute("""SELECT site, %s AS person, post_url FROM %s
       WHERE post_url IS NOT NULL AND TRIM(post_url)<>'' LIMIT 0""" % (namecol, tbl)):
        pass
    print('--- %s 的 post_url 形态（去重前 3 条/站点）---' % tbl)
    for r in con.execute("""SELECT site, GROUP_CONCAT(DISTINCT substr(post_url,1,72)) u
       FROM (SELECT site, post_url FROM %s ORDER BY rowid DESC LIMIT 300)
       GROUP BY site""" % tbl):
        print('  %-10s %s' % (r['site'], (r['u'] or '')[:220]))

print()
print('=' * 78)
print('四、players 是否真的有主页（人这一层的入口）')
print('=' * 78)
for r in con.execute("""SELECT site, name, channel, source_url FROM players
   WHERE source_url IS NOT NULL AND TRIM(source_url)<>'' LIMIT 12"""):
    print('  %-10s %-16s %-4s %s' % (r['site'], (r['name'] or '')[:16], r['channel'], r['source_url'][:78]))

print()
print('=' * 78)
print('五、eastmoney：人的标识到底是什么（判断能否以人为轴）')
print('=' * 78)
for r in con.execute("""SELECT DISTINCT author FROM trades WHERE site='eastmoney' LIMIT 15"""):
    print('  author =', repr(r['author']))
print('  --- players 里 eastmoney 的 name 样例 ---')
for r in con.execute("""SELECT name, source_url FROM players WHERE site='eastmoney' LIMIT 12"""):
    print('   %-20s %s' % ((r['name'] or '')[:20], (r['source_url'] or '')[:70]))

con.close()
