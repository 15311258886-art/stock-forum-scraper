# -*- coding: utf-8 -*-
"""只读扫描：语料昵称里「…客户端 / …官微 / …服务号 / …官方 / …在线 / …工作室」等
机器号同形态的**漏判候选**（当前 is_bot_author 未命中者）。仅供 team-lead 复核，不改名单。
输出写 tools/_scan_bot_suffix.log。
"""
from __future__ import annotations
import collections, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scraper import bot_filter  # noqa: E402

CORPUS = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\players_posts.jsonl"
LOG = ROOT / "tools" / "_scan_bot_suffix.log"

# 关注的后缀形态（team-lead 点名 + 同类）
PATTERNS = ["客户端", "官微", "服务号", "官方", "在线", "工作室", "研究所", "资讯",
            "播报", "快讯", "财经", "观察", "频道", "媒体", "编辑"]


def main():
    log = open(LOG, "w", encoding="utf-8")
    sys.stdout = log

    texts = collections.defaultdict(list)
    site_of = {}
    with open(CORPUS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            n = r.get("nickname")
            if not n:
                continue
            texts[n].append(r.get("text") or "")
            site_of.setdefault(n, r.get("site", ""))

    print("语料 distinct 昵称 = %d" % len(texts))
    print("=" * 70)

    for pat in PATTERNS:
        hits = [n for n in texts if pat in n]
        # 排除已确证/已命中
        missed = []
        for n in hits:
            isb, why = bot_filter.is_bot_author(n)
            if not isb:
                missed.append(n)
        if not missed:
            continue
        print("\n【含 '%s' 且**当前未命中**的昵称：%d 个】" % (pat, len(missed)))
        for n in sorted(missed):
            txs = texts[n]
            res = bot_filter.classify_account(n, txs, site_of.get(n, ""))
            # 是否有第一人称买卖动词（判"真人嫌疑"）
            joined = " ".join(txs)
            has_trade = any(v in joined for v in bot_filter._TRADE_VERBS)
            print("  - %-20s [%s] 帖=%2d  内容判=%s/%s  含买卖动词=%s"
                  % (n, site_of.get(n, ""), len(txs),
                     res["is_bot"], res["confidence"], has_trade))

    print("\n" + "=" * 70)
    print("当前 BOT_NAMES（%d 个）：%s" % (len(bot_filter.BOT_NAMES), "、".join(bot_filter.BOT_NAMES)))


if __name__ == "__main__":
    main()
