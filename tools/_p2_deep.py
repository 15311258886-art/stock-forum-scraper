# -*- coding: utf-8 -*-
"""P2 深挖：看 9 行脏名对应的原帖文本 + 词典条目。只读。"""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = Path(r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据")
log = open(ROOT / "tools" / "_p2_deep.log", "w", encoding="utf-8")
sys.stdout = log

# 词典里 000688 / 000001 的全部条目
d = json.loads((ROOT / "scraper" / "stock_names_full.json").read_text(encoding="utf-8"))
for code in ("000688", "000001", "000905", "399001", "399006"):
    print("代码 %s 的词典条目: %s" % (code, [k for k, v in d.items() if str(v) == code]))

print("\n" + "=" * 70)
# 找到目标 pid 对应语料原帖
pids = {"2vkcJb6jkT5", "2viy3EE9Fij", "2vJ5UpiH3ZZ", "2vJ3FgINP1P", "2tiyupgk0Q3"}
for line in (BASE / "players_posts.jsonl").read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    r = json.loads(line)
    seg = str(r.get("post_url") or "").rsplit("/", 1)[-1]
    if seg in pids:
        txt = r.get("text") or ""
        print("\npid=%s site=%s author=%s pub=%s" % (seg, r.get("site"), r.get("nickname"), r.get("publish_time")))
        print("  text[:400] =", txt[:400])
