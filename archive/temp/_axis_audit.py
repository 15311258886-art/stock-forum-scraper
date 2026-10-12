# -*- coding: utf-8 -*-
"""全站抓取轴审计：人轴 vs 帖轴
判定标准：
  A) 人轴：抓取入口是「人的主页/uid」，持仓与操作从人的页面取；帖子只作印证
  B) 帖轴：抓取入口是「帖子/列表」，人是从帖子里反推出来的
输出：每站的「人的标识可得性」「当前持仓来源URL形态」「帖子URL形态」
"""
import os, re, sqlite3, collections, json

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(BASE, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

OUT = []
def P(*a):
    s = ' '.join(str(x) for x in a)
    OUT.append(s)

P('=' * 72)
P('全站抓取轴审计  %s' % __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M'))
P('=' * 72)

# ---------- 1. players：人是否有主页/uid ----------
P()
P('【1】players 表：每个站点的「人」是否带主页 URL')
P('%-12s %6s %8s %8s %8s  %s' % ('site', '人数', '有URL', 'URL率', '有channel', 'URL 样例'))
for r in con.execute("""SELECT site, COUNT(*) n,
        SUM(CASE WHEN source_url IS NOT NULL AND TRIM(source_url)<>'' THEN 1 ELSE 0 END) with_url
    FROM players GROUP BY site ORDER BY n DESC"""):
    site, n, wu = r['site'], r['n'], r['with_url']
    rate = '%.0f%%' % (wu * 100.0 / n) if n else '-'
    samp = con.execute("""SELECT source_url FROM players WHERE site=? AND source_url IS NOT NULL
        AND TRIM(source_url)<>'' LIMIT 1""", (site,)).fetchone()
    ch = con.execute("SELECT GROUP_CONCAT(DISTINCT channel) FROM players WHERE site=?", (site,)).fetchone()[0]
    P('%-12s %6s %8s %8s %8s  %s' % (site, n, wu, rate, ch or '', (samp[0][:56] if samp else '(无)')))

# ---------- 2. positions：持仓来源 URL 形态 ----------
P()
P('【2】positions 表：当前持仓的 post_url 指向「人主页」还是「帖子」')
for r in con.execute("SELECT site, COUNT(*) n FROM positions GROUP BY site ORDER BY n DESC"):
    site, n = r['site'], r['n']
    P('  --- %s (%d 条) ---' % (site, n))
    c = collections.Counter()
    for x in con.execute("SELECT post_url FROM positions WHERE site=?", (site,)):
        u = x['post_url'] or '(空)'
        # 归类
        if not u or u == '(空)':
            k = '(空)'
        elif re.search(r'/blog/\d+|/u/\d+|/user/\d+|/people/|/space/|/profile', u):
            k = '人主页形态'
        elif re.search(r'/news,|/post|/status|/a/\d+|tid=|/circle/|/thread|/topic', u):
            k = '帖子形态'
        else:
            k = '其他: ' + u[:44]
        c[k] += 1
    for k, v in c.most_common(5):
        P('      %-46s %d' % (k, v))

# ---------- 3. trades：交易来源 ----------
P()
P('【3】trades 表：交易(操作)来源 URL 形态')
for r in con.execute("SELECT site, COUNT(*) n FROM trades GROUP BY site ORDER BY n DESC"):
    site, n = r['site'], r['n']
    c = collections.Counter()
    for x in con.execute("SELECT post_url FROM trades WHERE site=?", (site,)):
        u = x['post_url'] or ''
        if re.search(r'/blog/\d+|/u/\d+|/user/\d+|i\.eastmoney\.com/\d+|/people/|/space/', u):
            k = '人主页形态'
        elif re.search(r'/news,|/post|/status|tid=|/circle/|guba', u):
            k = '帖子形态'
        elif not u:
            k = '(空)'
        else:
            k = '其他: ' + u[:40]
        c[k] += 1
    P('  %-12s n=%-4s | %s' % (site, n, ' / '.join('%s:%d' % (k, v) for k, v in c.most_common(4))))

# ---------- 4. 帖子表(post/news) 是否带作者uid ----------
P()
P('【4】posts/news 表：作者是否带 uid（决定能否从帖反查人）')
for t in ('posts', 'news'):
    cols = [r[1] for r in con.execute('PRAGMA table_info(%s)' % t)]
    P('  %s 列: %s' % (t, cols))
    uid_cols = [c for c in cols if 'uid' in c.lower() or 'author' in c.lower()]
    P('    疑似人标识列: %s' % (uid_cols or '(无)'))

con.close()
open(os.path.join(BASE, '_axis_audit_out.txt'), 'w', encoding='utf-8').write('\n'.join(OUT))
print('\n'.join(OUT))
