# -*- coding: utf-8 -*-
"""只读：扫描 viewer/app.db 的 posts/trades 作者名，找「…客户端/官微/服务号/官方/号/资讯/播报」
等机器号同形态、且当前 is_bot_author 未命中的漏判候选。mode=ro，绝不写。
输出 tools/_scan_db_authors.log。
"""
from __future__ import annotations
import sys, sqlite3, collections
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scraper import bot_filter
DB = ROOT / "viewer" / "app.db"
log = open(ROOT / "tools" / "_scan_db_authors.log", "w", encoding="utf-8")
sys.stdout = log

PATTERNS = ["客户端", "官微", "服务号", "官方", "资讯", "播报", "快讯", "财经", "观察",
            "频道", "媒体", "在线", "工作室", "研究所", "团队"]

con = sqlite3.connect("file:%s?mode=ro" % str(DB).replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()

# 汇总 posts+trades 里每个作者的帖数
cnt = collections.Counter()
site_of = {}
for t, acol in (("posts", "author"), ("trades", "author")):
    for r in cur.execute("SELECT %s a, site s, COUNT(*) c FROM %s GROUP BY 1,2" % (acol, t)):
        cnt[r["a"]] += r["c"]
        site_of.setdefault(r["a"], r["s"])
print("app.db posts+trades distinct 作者 = %d" % len(cnt))
print("=" * 72)
for pat in PATTERNS:
    rows = [(n, cnt[n]) for n in cnt if pat in n and not bot_filter.is_bot_author(n)[0]]
    if not rows:
        continue
    print("\n【含 '%s' 且**当前未命中**：%d 个】" % (pat, len(rows)))
    for n, c in sorted(rows, key=lambda x: -x[1]):
        res = bot_filter.classify_account(n, [], site_of.get(n, ""))
        print("  - %-24s [%-9s] 帖=%4d  内容判(空文本)=%s/%s" %
              (n, site_of.get(n, ""), c, res["is_bot"], res["confidence"]))

print("\n" + "=" * 72)
print("已命中(确证名单 8)在本库的帖数：")
for n in bot_filter.BOT_NAMES:
    print("  - %-16s 帖=%d" % (n, cnt.get(n, 0)))

# 2 笔 HOLD 明细
print("\n'新浪财经客户端' 在 trades 的 2 行：")
for r in cur.execute("SELECT site,action,symbol,symbol_name,post_id FROM trades WHERE author=?",
                     ("新浪财经客户端",)):
    print("  - %s %s %s %s pid=%s" % (r["site"], r["action"], r["symbol"], r["symbol_name"], r["post_id"]))
con.close()
