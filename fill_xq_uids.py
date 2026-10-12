# -*- coding: utf-8 -*-
"""把解析到的 34 个雪球 uid 回填：① xueqiu_targets.json ② players.source_url"""
import os, json, sqlite3, shutil, datetime

BASE = r'F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper'
DB = os.path.join(BASE, 'viewer', 'app.db')

cubes = json.load(open(os.path.join(BASE, 'xueqiu_cubes.json'), encoding='utf-8'))
players_map = cubes.get('players', {}) or {}
uid_by_name = {}
for uid, p in players_map.items():
    nm = (p.get('name') or '').strip()
    cc = p.get('cube_count')
    if nm:
        uid_by_name[nm] = (str(uid), cc)
print('解析到 uid 的人名数:', len(uid_by_name))
print('其中 cube_count>0 的:', [(n, v[1]) for n, v in uid_by_name.items() if v[1]])

# ---- 1) 回填 xueqiu_targets.json ----
tpath = os.path.join(BASE, 'xueqiu_targets.json')
tg = json.load(open(tpath, encoding='utf-8'))
filled = 0
for t in tg:
    nm = (t.get('name') or '').strip()
    if not t.get('uid') and nm in uid_by_name:
        t['uid'] = uid_by_name[nm][0]
        filled += 1
shutil.copyfile(tpath, tpath + '.bak_before-uidfill')
json.dump(tg, open(tpath, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('targets 回填 uid:', filled, '/', len(tg))

# ---- 2) 写回 players.source_url（先备份 DB）----
bak = DB + '.bak.before-uidfill-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
shutil.copyfile(DB, bak)
print('DB 已备份 ->', os.path.basename(bak))

con = sqlite3.connect(DB)
cur = con.cursor()
upd = 0
for nm, (uid, cc) in uid_by_name.items():
    url = f'https://xueqiu.com/u/{uid}'
    cur.execute(
        "UPDATE players SET source_url=? WHERE site='xueqiu' AND name=? "
        "AND (source_url IS NULL OR source_url='')", (url, nm))
    upd += cur.rowcount
con.commit()

cur.execute("SELECT COUNT(*) FROM players WHERE site='xueqiu' AND source_url LIKE '%xueqiu.com/u/%'")
print('players 已带 uid 主页链接:', cur.fetchone()[0], '/ 34')
print('本次新写入 source_url:', upd)

# ---- 3) 现状汇总 ----
print('\n--- 雪球数据现状 ---')
for t in ['players', 'positions', 'trades', 'news', 'posts']:
    try:
        cur.execute(f"SELECT COUNT(*) FROM {t} WHERE site='xueqiu'")
        print(f'  {t}: {cur.fetchone()[0]}')
    except Exception as e:
        print(f'  {t}: ERR {e}')
con.close()
