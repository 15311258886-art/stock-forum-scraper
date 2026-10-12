# -*- coding: utf-8 -*-
"""QA(Edward) 第二轮复验 · 第5项：误杀复核。
553 昵称(players 326 ∪ 名单B 227) 跑 viewer.db.is_bot_author 与 scraper.bot_filter.is_bot_author。"""
from __future__ import annotations
import csv, json, sqlite3, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from viewer.db import is_bot_author as viewer_bot, _BOT_NAMES_VIEWER_EXTRA  # noqa: E402
from scraper.bot_filter import is_bot_author as scraper_bot                 # noqa: E402

DB = ROOT / "viewer" / "app.db"
LIST_B = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\名单B_选手主页URL.csv"
CORPUS = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\players_posts.jsonl"

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
names_db = [r[0] for r in con.execute("SELECT DISTINCT name FROM players WHERE name IS NOT NULL")]
con.close()
nb = set()
with open(LIST_B, encoding="utf-8-sig") as f:
    rd = list(csv.reader(f))
    idx = rd[0].index("选手昵称")
    for r in rd[1:]:
        if len(r) > idx:
            nb.add(r[idx].strip())
cand = sorted(set(names_db) | nb)
print(f"候选昵称 = players({len(set(names_db))}) ∪ 名单B({len(nb)}) = {len(cand)}")
print("viewer 额外兜底名单:", _BOT_NAMES_VIEWER_EXTRA)
print()

vf = [(n, "viewer") for n in cand if viewer_bot(n)]
sf = [(n, "scraper") for n in cand if scraper_bot(n)[0]]
vset = {n for n, _ in vf}
sset = {n for n, _ in sf}
print(f"viewer 判定命中 = {len(vset)}；scraper 判定命中 = {len(sset)}")
print(f"  viewer 命中: {sorted(vset)}")
print(f"  scraper 命中: {sorted(sset)}")
print(f"  仅 viewer 命中(新增兜底): {sorted(vset - sset)}")
print(f"  仅 scraper 命中: {sorted(sset - vset)}")
print()

# 判定每个命中者：是否真人（在名单B=真人选手池；不在则看其 site/axis/语料）
print("命中者画像（来源 / axis / 是否在名单B真选手里）:")
for n in sorted(vset | sset):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    rows = con.execute("SELECT site,axis,tier FROM players WHERE name=?", (n,)).fetchall()
    con.close()
    print(f"  {n:20s} 在名单B={'Y' if n in nb else 'N'}  players行={rows}")

print()
print("被新增兜底「新浪财经客户端」的语料抽样（判断是否机器号）:")
ps = []
with open(CORPUS, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("nickname") == "新浪财经客户端":
            ps.append(r)
print(f"  语料帖数={len(ps)}")
for r in ps[:6]:
    print(f"    [{r.get('site')}] {(r.get('text') or '')[:110]}")
print(f"  是否在名单B真选手里: {'Y' if '新浪财经客户端' in nb else 'N'}")
