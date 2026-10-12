# -*- coding: utf-8 -*-
"""跨平台选手身份匹配实证（只读）
目的：验证「同一人在多个平台都有账号」的可行性与覆盖度。
"""
import sqlite3, os, re, collections, json

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

out = []
def P(*a):
    s = ' '.join(str(x) for x in a)
    out.append(s)
    print(s)

# ---------- 1) players 各站点人数 ----------
site_names = collections.defaultdict(set)
for r in con.execute("SELECT site, name FROM players WHERE name IS NOT NULL AND TRIM(name)<>''"):
    site_names[r['site']].add(r['name'].strip())

P('=== [1] players 各站点选手数 ===')
for k, v in sorted(site_names.items(), key=lambda kv: -len(kv[1])):
    P('  %-12s %d' % (k, len(v)))

# ---------- 2) 有持仓的选手 ----------
pos_names = collections.defaultdict(set)
for r in con.execute("SELECT site, player FROM positions WHERE player IS NOT NULL AND TRIM(player)<>''"):
    pos_names[r['site']].add(r['player'].strip())
P('')
P('=== [2] 有持仓记录的选手（positions） ===')
for k, v in sorted(pos_names.items(), key=lambda kv: -len(kv[1])):
    P('  %-12s %d' % (k, len(v)))

# ---------- 3) 精确同名 ----------
sites = sorted(site_names.keys())
P('')
P('=== [3] 跨站点精确同名 ===')
hit_exact = []
for i in range(len(sites)):
    for j in range(i + 1, len(sites)):
        a, b = sites[i], sites[j]
        inter = site_names[a] & site_names[b]
        if inter:
            hit_exact.append((a, b, sorted(inter)))
            P('  %s ∩ %s : %d 人' % (a, b, len(inter)))
            for n in sorted(inter)[:25]:
                P('       -', n)
if not hit_exact:
    P('  无精确同名（说明平台间昵称差异大，需要归一化匹配）')

# ---------- 4) 归一化匹配 ----------
def norm(s):
    s = s.strip()
    s = re.sub(r'[\s_\-·\.]+', '', s)
    s = re.sub(r'[（(].*?[)）]', '', s)          # 去括号注释
    s = re.sub(r'[0-9]+$', '', s)               # 去尾部数字
    s = re.sub(r'(v|V|VIP|vip|的|股|哥|姐|叔|爷)$', '', s)  # 去常见尾缀
    s = re.sub(r'[^\w\u4e00-\u9fff]', '', s)    # 去符号
    return s

normmap = collections.defaultdict(lambda: collections.defaultdict(set))
for site, names in site_names.items():
    for n in names:
        k = norm(n)
        if len(k) >= 2:
            normmap[k][site].add(n)

P('')
P('=== [4] 归一化后跨站点匹配 ===')
multi = []
for k, m in sorted(normmap.items()):
    if len(m) >= 2:
        multi.append((k, m))
for k, m in multi[:50]:
    P('  「%s」 -> %s' % (k, ' | '.join('%s:%s' % (s, '/'.join(sorted(v))) for s, v in m.items())))
P('  合计跨站匹配键：%d 个' % len(multi))

# ---------- 5) 匹配者的持仓覆盖（能不能互补） ----------
P('')
P('=== [5] 跨站匹配选手的持仓/交易分布（判断互补潜力） ===')
useful = []
for k, m in multi:
    rows = []
    for s, v in m.items():
        for n in v:
            c = con.execute("SELECT COUNT(*) FROM positions WHERE site=? AND player=?", (s, n)).fetchone()[0]
            t = con.execute("SELECT COUNT(*) FROM trades WHERE site=? AND author=?", (s, n)).fetchone()[0]
            if c or t:
                rows.append((s, n, c, t))
    if rows and len(set(r[0] for r in rows)) >= 2:
        useful.append((k, rows))
if useful:
    for k, rows in useful[:30]:
        P('  「%s」' % k)
        for s, n, c, t in rows:
            P('      %-10s %-14s 持仓=%-4s 交易=%s' % (s, n, c, t))
else:
    P('  暂无可互补的跨站匹配（两边都有数据的）')

# ---------- 6) 同一代码被多平台提及（另一种印证维度） ----------
P('')
P('=== [6] 同一股票被多平台同时操作（信号印证维度） ===')
q = """
SELECT symbol, COUNT(DISTINCT site) ns, COUNT(*) n,
       GROUP_CONCAT(DISTINCT site) sites
FROM trades WHERE symbol IS NOT NULL AND TRIM(symbol)<>''
GROUP BY symbol HAVING ns>=2 ORDER BY ns DESC, n DESC LIMIT 25
"""
for r in con.execute(q):
    P('  %s  被 %d 个平台操作  共%d条  [%s]' % (r['symbol'], r['ns'], r['n'], r['sites']))

P('')
P('=== [7] positions 里同一代码被多平台持有 ===')
q2 = """
SELECT symbol, COUNT(DISTINCT site) ns, COUNT(DISTINCT player) np, COUNT(*) n,
       GROUP_CONCAT(DISTINCT site) sites
FROM positions WHERE symbol IS NOT NULL AND TRIM(symbol)<>''
GROUP BY symbol HAVING ns>=2 ORDER BY ns DESC, n DESC LIMIT 25
"""
for r in con.execute(q2):
    P('  %s  被 %d 平台 / %d 人持有  共%d条  [%s]' % (r['symbol'], r['ns'], r['np'], r['n'], r['sites']))

# ---------- 8) players 里是否有跨站同名的 source_url 可交叉验证 ----------
P('')
P('=== [8] 各站点选手 source_url 覆盖率 ===')
for r in con.execute("""SELECT site, COUNT(*) n,
       SUM(CASE WHEN source_url IS NULL OR TRIM(source_url)='' THEN 1 ELSE 0 END) blank
       FROM players GROUP BY site ORDER BY n DESC"""):
    P('  %-12s n=%-5s 无URL=%-5s' % (r['site'], r['n'], r['blank']))

con.close()
open(os.path.join(B, '_xmatch_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n[written] _xmatch_out.txt')
