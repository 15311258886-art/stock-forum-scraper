# -*- coding: utf-8 -*-
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scraper import bot_filter

CORPUS = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\players_posts.jsonl"
log = open(ROOT / "tools" / "_probe_xlc.py.log", "w", encoding="utf-8")
sys.stdout = log

print("is_bot_author('新浪财经客户端') =", bot_filter.is_bot_author("新浪财经客户端"))
print("classify_account('新浪财经客户端', []) =",
      bot_filter.classify_account("新浪财经客户端", [], "sina"))

n_lines = 0
nick_hits = 0
sites = {}
nicks = {}
for line in open(CORPUS, encoding="utf-8"):
    if "新浪财经客户端" not in line:
        continue
    n_lines += 1
    r = json.loads(line)
    nk = r.get("nickname", "")
    sites[r.get("site", "")] = sites.get(r.get("site", ""), 0) + 1
    nicks[nk] = nicks.get(nk, 0) + 1
    if "新浪财经客户端" in (nk or ""):
        nick_hits += 1
print("含'新浪财经客户端'的行 =", n_lines, " 其中 nickname 含该串 =", nick_hits)
print("site 分布 =", sites)
print("这些行的 nickname 分布 =", nicks)
print("样例 text[:80]:")
for line in list(open(CORPUS, encoding="utf-8")):
    if "新浪财经客户端" in line:
        r = json.loads(line)
        print("   nickname=%r  text=%r" % (r.get("nickname"), (r.get("text") or "")[:80]))
        break
# 列出所有 nickname 里含"客户端"/"官微"/"服务号"/"官方"的
allnk = set()
for line in open(CORPUS, encoding="utf-8"):
    r = json.loads(line)
    nk = r.get("nickname") or ""
    for p in ("客户端", "官微", "服务号", "官方", "号"):
        if p in nk:
            allnk.add((nk, r.get("site", "")))
print("\n所有含 客户端/官微/服务号/官方/号 的 nickname:")
for nk, s in sorted(allnk):
    print("  -", s, nk)
