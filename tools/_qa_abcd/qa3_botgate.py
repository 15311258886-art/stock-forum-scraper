# -*- coding: utf-8 -*-
"""QA(Edward) 独立验证 · 第3项：A 机器号 gate 零误杀核查（关键）。"""
from __future__ import annotations
import csv, json, sqlite3, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from scraper.bot_filter import BOT_NAMES, is_bot_author, classify_account  # noqa: E402

DB = ROOT / "viewer" / "app.db"
LIST_B = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\名单B_选手主页URL.csv"
CORPUS = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\players_posts.jsonl"

print("#" * 90)
print("# 1) 确证名单（BOT_NAMES，即 quality_filter[2b] 实际写入 axis=bot 的名单）")
print("#" * 90)
for n, why in BOT_NAMES.items():
    print(f"  {n:14s} :: {why}")

print()
print("#" * 90)
print("# 2) 反向测试：把昵称正则 + 名单 跑一遍全部真人选手昵称")
print("#" * 90)
# 2a) DB players 326
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
names_db = [r[0] for r in con.execute("SELECT DISTINCT name FROM players WHERE name IS NOT NULL")]
con.close()
# 2b) 名单B
names_b = []
with open(LIST_B, encoding="utf-8-sig") as f:
    rd = csv.reader(f)
    rows = list(rd)
print("  名单B 表头:", rows[0] if rows else "(空)")
for r in rows[1:]:
    if r:
        for cell in r:
            c = (cell or "").strip()
            if c and not c.startswith("http") and len(c) <= 30 and "URL" not in c and "主页" not in c:
                names_b.append(c)
                break

cand = sorted(set(names_db) | set(names_b))
print(f"  DB players 去重昵称={len(set(names_db))}；名单B 昵称={len(set(names_b))}；合计候选={len(cand)}")
flagged = []
for n in cand:
    hit, why = is_bot_author(n)
    if hit:
        flagged.append((n, why))
print(f"  被 is_bot_author 判为机器号的真人候选 = {len(flagged)} 个  <<< 误杀红线")
for n, why in flagged:
    print(f"      !!! {n:24s} | {why}")

print()
print("#" * 90)
print("# 3) 被 gate 的确证机器号：抽样其帖子，人工确认为非真人")
print("#" * 90)
posts = {}
with open(CORPUS, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("nickname") in BOT_NAMES:
            posts.setdefault(r["nickname"], []).append(r)
for n in list(BOT_NAMES)[:5]:
    ps = posts.get(n, [])
    print("-" * 80)
    print(f"  {n}：语料中 {len(ps)} 帖")
    for r in ps[:2]:
        print(f"     [{r['site']}] {(r.get('text') or '')[:120]}")
        if r.get("post_url"):
            print(f"        url={r['post_url']}")

print()
print("#" * 90)
print("# 4) 补充：hot-path gate 也会抓到的「XXX资讯」类作者（不在 BOT_NAMES，但正则命中）")
print("#" * 90)
for n in ["宁德时代资讯", "贵州茅台资讯", "平安银行资讯", "中国平安资讯"]:
    print(f"  is_bot_author({n!r}) = {is_bot_author(n)}")
