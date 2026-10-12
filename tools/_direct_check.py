# -*- coding: utf-8 -*-
from __future__ import annotations
import json, sys, os, collections
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
STAGING = Path(r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\trades_v2_staging.jsonl")
log = open(ROOT / "tools" / "_direct_check.log", "w", encoding="utf-8")
sys.stdout = log
print("mtime:", __import__("time").strftime("%Y-%m-%d %H:%M:%S", __import__("time").localtime(os.path.getmtime(STAGING))))
rows = [json.loads(l) for l in STAGING.read_text(encoding="utf-8").splitlines() if l.strip()]
print("行数:", len(rows))
pt = collections.Counter(r.get("post_time") for r in rows)
print("distinct post_time:", len(pt), " top5:", pt.most_common(5))
for s in ("000688", "000001"):
    print("symbol=%s:" % s, [(r.get("symbol_name"), r.get("post_id")) for r in rows if r.get("symbol") == s])
blanks = [r for r in rows if not (r.get("symbol_name") or "").strip()]
print("空白 symbol_name:", len(blanks), [(r.get("site"), r.get("symbol")) for r in blanks])
print("样例3行:")
for r in rows[:3]:
    print("  ", {k: r.get(k) for k in ("site", "author", "post_id", "post_time", "action", "symbol", "symbol_name")})
