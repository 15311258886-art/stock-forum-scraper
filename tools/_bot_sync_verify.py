# -*- coding: utf-8 -*-
from __future__ import annotations
import csv, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scraper import bot_filter
log = open(ROOT / "tools" / "_bot_sync_verify.log", "w", encoding="utf-8")
sys.stdout = log
print("BOT_NAMES 个数 =", len(bot_filter.BOT_NAMES))
print("is_bot_author('新浪财经客户端') =", bot_filter.is_bot_author("新浪财经客户端"))
print("is_bot_author('新浪财经客') =", bot_filter.is_bot_author("新浪财经客"))
print("is_bot_author('三友化工小') =", bot_filter.is_bot_author("三友化工小"))
print("[后缀正则自检] is_bot_author('头条客户端') =", bot_filter.is_bot_author("头条客户端"))
print("[后缀正则自检] is_bot_author('XX服务号') =", bot_filter.is_bot_author("XX服务号"))
print("[后缀正则自检] is_bot_author('普通小散') =", bot_filter.is_bot_author("普通小散"))
CSV = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\bot_accounts.csv"
rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
print("CSV 行数 =", len(rows))
for r in rows:
    if r["nickname"] == "新浪财经客户端":
        print("CSV 该行 =", r)
