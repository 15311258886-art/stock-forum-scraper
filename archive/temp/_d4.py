# -*- coding: utf-8 -*-
"""评估：持仓能否用同选手的帖子/交易时间补出「建仓日期」"""
import sqlite3, os, re, json

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

print('=== 1) 各站持仓：已有日期 vs 无日期 ===')
for r in con.execute("""SELECT site, COUNT(*) n,
     SUM(CASE WHEN (hold_since IS NOT NULL AND TRIM(hold_since)<>'') THEN 1 ELSE 0 END) hs,
     SUM(CASE WHEN (buy_time IS NOT NULL AND TRIM(buy_time)<>'') THEN 1 ELSE 0 END) bt,
     SUM(CASE WHEN (post_time IS NOT NULL AND TRIM(post_time)<>'') THEN 1 ELSE 0 END) pt
     FROM positions GROUP BY site"""):
    print('  %-10s n=%-4s hold_since=%-3s buy_time=%-3s post_time=%s' % (r['site'], r['n'], r['hs'], r['bt'], r['pt']))

print()
print('=== 2) taoguba 持仓：同选手在 posts 里是否有帖子 ===')
q = """SELECT p.player, COUNT(*) n_hold,
       (SELECT COUNT(*) FROM posts x WHERE x.site='taoguba' AND x.author=p.player) n_post
       FROM positions p WHERE p.site='taoguba' GROUP BY p.player"""
rows = con.execute(q).fetchall()
with_post = [r for r in rows if r['n_post'] > 0]
print('  选手数 %d，其中有帖子的 %d' % (len(rows), len(with_post)))
print('  这些选手的持仓条数合计: %d' % sum(r['n_hold'] for r in with_post))

print()
print('=== 3) 帖子正文里能否命中该持仓的 symbol_name ===')
# 取这些选手的帖子文本
hit_total = 0
hit_detail = []
for r in with_post[:200]:
    player = r['player']
    posts = con.execute("""SELECT post_time, text FROM posts
        WHERE site='taoguba' AND author=? AND text IS NOT NULL AND TRIM(text)<>''
        ORDER BY post_time DESC LIMIT 30""", (player,)).fetchall()
    if not posts:
        continue
    holds = con.execute("""SELECT symbol, symbol_name FROM positions
        WHERE site='taoguba' AND player=?""", (player,)).fetchall()
    for h in holds:
        nm = (h['symbol_name'] or '').strip()
        if len(nm) < 2:
            continue
        for p in posts:
            if nm in (p['text'] or ''):
                hit_total += 1
                if len(hit_detail) < 15:
                    hit_detail.append((player, h['symbol'], nm, p['post_time'], (p['text'] or '')[:70]))
                break

print('  可按「同选手帖子提到该股名」补出时间的持仓条数: %d' % hit_total)
print()
for d in hit_detail:
    print('   %-14s %s %-8s -> %s | %s' % (d[0][:14], d[1], d[2], d[3], d[4]))

print()
print('=== 4) trades 表里 taoguba 带时间的记录（同选手+同代码）===')
q2 = """SELECT COUNT(*) FROM positions p WHERE p.site='taoguba'
   AND EXISTS(SELECT 1 FROM trades t WHERE t.site='taoguba' AND t.author=p.player
              AND t.symbol=p.symbol AND t.post_time IS NOT NULL AND TRIM(t.post_time)<>'')"""
print('  可补:', con.execute(q2).fetchone()[0])
for r in con.execute("""SELECT author, symbol, symbol_name, action, post_time FROM trades
     WHERE site='taoguba' ORDER BY post_time DESC LIMIT 12"""):
    print('   %-16s %s %-8s %-5s %s' % ((r['author'] or '')[:16], r['symbol'], (r['symbol_name'] or '')[:8], r['action'], r['post_time']))

con.close()
