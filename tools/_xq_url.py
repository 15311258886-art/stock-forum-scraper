# -*- coding: utf-8 -*-
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
BASE = Path(r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据")
log = open(ROOT / "tools" / "_xq_url.log", "w", encoding="utf-8")
sys.stdout = log
n = 0
for line in (BASE / "players_posts.jsonl").read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    r = json.loads(line)
    if r.get("site") == "xueqiu":
        print("nickname=%r post_url=%r publish_time=%r home_url=%r" %
              (r.get("nickname"), r.get("post_url"), r.get("publish_time"), r.get("home_url")))
        n += 1
        if n >= 8:
            break
