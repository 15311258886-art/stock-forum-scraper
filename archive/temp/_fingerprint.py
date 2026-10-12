# -*- coding: utf-8 -*-
"""跨平台「持仓指纹」印证实证（只读）
思路：同一个人在 A/B 两个平台若都留下持仓，其持仓集合会高度重合。
用 Jaccard 相似度找跨平台强候选，比昵称可靠（昵称可以不同，行为很难伪装）。
"""
import sqlite3, os, re, collections, itertools

B = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(B, 'viewer', 'app.db')
con = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
con.row_factory = sqlite3.Row

out = []
def P(*a):
    s = ' '.join(str(x) for x in a)
    out.append(s); print(s)

# ---- 1) 每个 (site, player) 的持仓代码集合 ----
hold = collections.defaultdict(set)
for r in con.execute("""SELECT site, player, symbol FROM positions
                        WHERE symbol IS NOT NULL AND TRIM(symbol)<>''"""):
    hold[(r['site'], r['player'])].add(r['symbol'])

# ---- 2) 交易涉及的代码（作为第二指纹） ----
trade = collections.defaultdict(set)
for r in con.execute("""SELECT site, author, symbol FROM trades
                        WHERE symbol IS NOT NULL AND TRIM(symbol)<>''"""):
    trade[(r['site'], r['author'])].add(r['symbol'])

P('=== [1] 各站点「有持仓集合」的选手数 / 平均持仓数 ===')
by_site = collections.defaultdict(list)
for (s, p), codes in hold.items():
    by_site[s].append((p, codes))
for s in sorted(by_site, key=lambda x: -len(by_site[x])):
    lst = by_site[s]
    avg = sum(len(c) for _, c in lst) / float(len(lst))
    P('  %-12s 选手 %-4d  平均持仓 %.1f 只' % (s, len(lst), avg))

P('')
P('=== [2] 跨平台持仓指纹匹配（Jaccard >= 0.34 且交集 >= 3）===')
keys = list(hold.keys())
cands = []
for (s1, p1), (s2, p2) in itertools.combinations(keys, 2):
    if s1 == s2:
        continue
    a, b = hold[(s1, p1)], hold[(s2, p2)]
    if len(a) < 3 or len(b) < 3:
        continue
    inter = a & b
    if len(inter) < 3:
        continue
    j = len(inter) / float(len(a | b))
    if j >= 0.34:
        cands.append((j, s1, p1, s2, p2, sorted(inter)))
cands.sort(reverse=True)
for j, s1, p1, s2, p2, inter in cands[:25]:
    P('  J=%.2f  [%s]%s  ∩  [%s]%s   重合 %d 只: %s'
      % (j, s1, p1, s2, p2, len(inter), '/'.join(inter[:10])))
if not cands:
    P('  无跨平台持仓指纹匹配')

P('')
P('=== [3] 放宽：跨平台「持仓+交易」合并指纹（交集 >= 2）===')
full = collections.defaultdict(set)
for k, v in hold.items():
    full[k] |= v
for k, v in trade.items():
    full[k] |= v
fkeys = [(k, v) for k, v in full.items() if len(v) >= 2]
cands2 = []
for (s1, p1), v1 in fkeys:
    for (s2, p2), v2 in fkeys:
        if s1 >= s2:
            continue
        inter = v1 & v2
        if len(inter) < 2:
            continue
        j = len(inter) / float(len(v1 | v2))
        cands2.append((j, s1, p1, s2, p2, sorted(inter)))
cands2.sort(reverse=True)
for j, s1, p1, s2, p2, inter in cands2[:20]:
    P('  J=%.2f  [%s]%s ∩ [%s]%s  重合 %s' % (j, s1, p1, s2, p2, '/'.join(inter[:10])))
if not cands2:
    P('  无匹配')

P('')
P('=== [4] 结论性统计：跨平台可印证覆盖率 ===')
P('  有持仓集合的选手总数：%d' % len(hold))
P('  涉及平台数：%d' % len(by_site))
P('  持仓指纹强匹配对：%d' % len(cands))
P('  合并指纹匹配对：%d' % len(cands2))
P('')
P('=== [5] 站内交叉：同一选手「持仓」与「交易」重合度（验证数据自洽）===')
n_self = 0
for (s, p) in hold:
    a, b = hold[(s, p)], trade.get((s, p), set())
    if a and b:
        n_self += 1
P('  同时有持仓与交易的选手：%d' % n_self)
for (s, p) in list(hold)[:6]:
    a, b = hold[(s, p)], trade.get((s, p), set())
    P('   [%s] %s  持仓%d 交易%d 交集%d' % (s, p, len(a), len(b), len(a & b)))

con.close()
open(os.path.join(B, '_fingerprint_out.txt'), 'w', encoding='utf-8').write('\n'.join(out))
print('\n[written] _fingerprint_out.txt')
