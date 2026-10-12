# -*- coding: utf-8 -*-
"""A：产出「机器资讯号」名单（昵称 + 命中依据 + 站点 + 置信度）供王总复核。

数据源：采集数据/players_posts.jsonl（按昵称聚合其全部帖子文本）。
判定：scraper/bot_filter.classify_account（昵称特征 + 内容特征）。
输出：stdout 表格 + 同目录 bot_accounts.csv。
只读，不写库。
"""
from __future__ import annotations
import csv, json, sys, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scraper import bot_filter  # noqa: E402

CORPUS = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\players_posts.jsonl"
OUTCSV = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\bot_accounts.csv"


def main():
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

    out = []
    for n, txs in texts.items():
        res = bot_filter.classify_account(n, txs, site_of.get(n, ""))
        if res["is_bot"] or (bot_filter.is_bot_author(n)[0]):
            out.append({
                "nickname": n, "site": site_of.get(n, ""), "n_posts": len(txs),
                "reason": res["reason"], "confidence": res["confidence"],
            })

    # 兜底：BOT_NAMES 里有、但语料没覆盖到的也列出
    for n, why in bot_filter.BOT_NAMES.items():
        if n not in {o["nickname"] for o in out}:
            out.append({"nickname": n, "site": "", "n_posts": 0,
                        "reason": f"确证名单：{why}", "confidence": "high"})

    out.sort(key=lambda x: (x["site"], x["nickname"]))
    print(f"机器/资讯号名单（共 {len(out)} 个）：")
    print(f"{'site':<8}{'nickname':<18}{'帖数':<6}{'置信':<8}reason")
    for o in out:
        print(f"{o['site']:<8}{o['nickname']:<18}{o['n_posts']:<6}{o['confidence']:<8}{o['reason']}")
    with open(OUTCSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["nickname", "site", "n_posts", "reason", "confidence"])
        w.writeheader()
        w.writerows(out)
    print(f"\n已写出: {OUTCSV}")


if __name__ == "__main__":
    main()
