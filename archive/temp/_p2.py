# -*- coding: utf-8 -*-
"""人轴端到端回测：逐站点实测「人主页」能否抓到持仓/操作线索"""
import os, re, sys, json, sqlite3, time

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
os.chdir(B)
sys.path.insert(0, B)
out = []
W = out.append

con = sqlite3.connect('file:' + os.path.join(B, 'viewer', 'app.db') + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

# ---------- 各站"人主页"URL 清单 ----------
W('========== 1) 各站人主页 URL 保存情况 ==========')
for site in ['taoguba', 'xueqiu', 'sina', 'eastmoney', 'ths', 'jisilu']:
    rows = con.execute("SELECT name, source_url FROM players WHERE site=? AND source_url IS NOT NULL AND TRIM(source_url)<>''", (site,)).fetchall()
    tot = con.execute("SELECT COUNT(*) FROM players WHERE site=?", (site,)).fetchone()[0]
    W('  %-10s 选手=%-4s 有主页URL=%-4s' % (site, tot, len(rows)))
    for r in rows[:3]:
        W('        %-18s %s' % ((r['name'] or '')[:18], r['source_url']))
W('')

# ---------- trades 里东财/新浪/雪球的可回溯线索 ----------
W('========== 2) 已入库的帖子线索（用于反推人主页）==========')
for site in ['eastmoney', 'sina', 'xueqiu', 'ths']:
    rows = con.execute("SELECT author, post_url, post_id FROM trades WHERE site=? LIMIT 3", (site,)).fetchall()
    W('  --- %s ---' % site)
    for r in rows:
        W('        author=%-14s id=%-22s url=%s' % ((r['author'] or '')[:14], r['post_id'], (r['post_url'] or '')[:78]))
W('')

con.close()
open(os.path.join(B, '_p2_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n'.join(out))
