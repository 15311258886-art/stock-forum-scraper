# -*- coding: utf-8 -*-
"""股东数据风险隔离：dry-run 预览（不落库）。
规则（按优先级）：
  controlling - 单只股票持股占比 >= 10%（举牌/实控人级别，不是二级市场投资者）
  family      - 同一股票同一报告期，同姓氏自然人 >= 3 人且该姓氏合计占比 >= 20%（家族持股）
  star        - 跨 >= 2 只股票出现的自然人（真牛散/游资级）
  single      - 其余（单股散户，无追踪价值）
"""
import os, sqlite3, collections, datetime

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(BASE, 'viewer', 'app.db')

def num(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    s = str(v).strip().replace(',', '').replace('%', '')
    if s in ('', '-', '--', '—', 'None', 'null'): return None
    try: return float(s)
    except ValueError: return None

con = sqlite3.connect(DB, timeout=40)
con.row_factory = sqlite3.Row
cur = con.cursor()

# ---------- 0) 备份 ----------
bak = DB + '.bak.before-holdertag-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
s = sqlite3.connect(DB); d = sqlite3.connect(bak); s.backup(d); d.close(); s.close()
print('[备份] %s  %.1f MB' % (os.path.basename(bak), os.path.getsize(bak) / 1048576))

# ---------- 1) 准备 ----------
cur.execute("PRAGMA table_info(stock_holders)")
cols = [r[1] for r in cur.fetchall()]
if 'holder_kind' not in cols:
    cur.execute("ALTER TABLE stock_holders ADD COLUMN holder_kind TEXT")
if 'exclude_reason' not in cols:
    cur.execute("ALTER TABLE stock_holders ADD COLUMN exclude_reason TEXT")
con.commit()
print('[结构] holder_kind / exclude_reason 已就绪')

rows = [dict(r) for r in cur.execute(
    "SELECT rowid AS rid, symbol, symbol_name, holder_name, holder_type, "
    "end_date, scope, holder_rank, hold_ratio, free_ratio, hold_change FROM stock_holders")]
print('[载入] %d 条股东记录' % len(rows))

# ---------- 2) 跨股统计（自然人） ----------
person_codes = collections.defaultdict(set)
for r in rows:
    if r['holder_type'] == '自然人' and r['holder_name']:
        person_codes[r['holder_name']].add(r['symbol'])
cross_n = {k: len(v) for k, v in person_codes.items()}

# ---------- 3) 按 (股票, 报告期) 找姓氏家族 ----------
by_grp = collections.defaultdict(list)
for r in rows:
    if r['holder_type'] == '自然人' and r['holder_name']:
        by_grp[(r['symbol'], r['end_date'])].append(r)

family_members = set()   # (rowid)
family_detail = []
for (sym, ed), lst in by_grp.items():
    if len(lst) < 3:
        continue
    sur = collections.defaultdict(list)
    for r in lst:
        nm = (r['holder_name'] or '').strip()
        if len(nm) >= 2:
            sur[nm[0]].append(r)
    for s_, grp in sur.items():
        if len(grp) >= 3:
            ratio_sum = sum((num(x['hold_ratio']) or 0) for x in grp)
            # 合计占比 >= 20% 才算家族控盘（防止同姓巧合）
            if ratio_sum >= 20:
                for x in grp:
                    family_members.add(x['rid'])
                family_detail.append((sym, lst[0]['symbol_name'], ed, s_, len(grp), round(ratio_sum, 2)))

# ---------- 4) 打标 ----------
stat = collections.Counter()
samples = collections.defaultdict(list)
for r in rows:
    rid = r['rid']
    ratio = num(r['hold_ratio'])
    is_person = (r['holder_type'] == '自然人')
    nm = (r['holder_name'] or '').strip()
    kind, reason = 'single', ''

    if is_person:
        if ratio is not None and ratio >= 10:
            kind, reason = 'controlling', '单股持股≥10%%（实控人/举牌级，非二级市场投资者）'
        elif rid in family_members:
            kind, reason = 'family', '同股同期同姓氏≥3人且合计≥20%%（家族持股）'
        elif cross_n.get(nm, 0) >= 2:
            kind, reason = 'star', ''
        else:
            kind, reason = 'single', ''
    else:
        kind, reason = 'org', ''

    stat[kind] += 1
    if len(samples[kind]) < 8:
        samples[kind].append('%s %s 占比%s 跨%s股 %s' % (
            r['symbol'], nm, r['hold_ratio'], cross_n.get(nm, 1), r['hold_change'] or ''))

print()
print('===== 打标分布 =====')
for k, v in stat.most_common():
    print('  %-12s %6d' % (k, v))
print()
print('===== 家族识别样例（前 12 组）=====')
for x in sorted(family_detail, key=lambda y: -y[5])[:12]:
    print('  %s %-8s %s 姓%s %s人 合计%.2f%%' % (x[0], (x[1] or '')[:8], x[2], x[3], x[4], x[5]))
print('  家族组数: %d（涉及记录 %d 条）' % (len(family_detail), len(family_members)))
print()
print('===== 各类样例 =====')
for k in ['controlling', 'family', 'star', 'single']:
    print('--- %s ---' % k)
    for x in samples[k][:6]:
        print('   ', x)

# ---------- 5) 牛散榜（star 级）预览 ----------
star_people = collections.Counter()
for r in rows:
    if r['holder_type'] != '自然人' or not r['holder_name']:
        continue
    rid = r['rid']
    nm = r['holder_name'].strip()
    ratio = num(r['hold_ratio'])
    if ratio is not None and ratio >= 10: continue
    if rid in family_members: continue
    if cross_n.get(nm, 0) >= 2:
        star_people[nm] += 1

print()
print('===== 隔离后「真牛散」榜（Top 15）=====')
for nm, n in star_people.most_common(15):
    codes = sorted(person_codes[nm])
    print('  %-12s %2s只  %s' % (nm[:12], n, ' '.join(codes[:8])))

# ---------- 6) 被隔离掉的对比（上一轮误入榜的） ----------
print()
print('===== 对比：隔离前后 牛散候选数 =====')
print('  隔离前（跨≥2只，未去污染）: %d 人' % sum(1 for k, v in cross_n.items() if v >= 2))
print('  隔离后（剔除实控人/家族）  : %d 人' % len(star_people))
con.close()
