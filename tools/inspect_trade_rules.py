# -*- coding: utf-8 -*-
"""诊断：规则④「名称 ↔ 动作词关联」旧 vs 新 —— 逐句展示"一个动作词认领了几只股"。

旧（before）：按**名**遍历，每个名找最近动作词（窗口 24）→ 一个动作词被多只股认领 → 1:N。
新（after） ：按**动作词**遍历，每个动作词只认领**最近 1 只**（距离 ≤ 15）→ 1:1。

只读 players_posts.jsonl 与 name_match；不发请求、不写库。
用法：.venv/Scripts/python.exe tools/inspect_trade_rules.py [作者1 作者2 ...]
"""
from __future__ import annotations
import json, sys, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scraper import name_match as nm  # noqa: E402
from scraper.parsers.taoguba import TaogubaParser, ACTION_MAP  # noqa: E402

CORPUS = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\players_posts.jsonl"
OLD_WINDOW = 24     # 旧：name_match.ASSOC_WINDOW
NEW_WINDOW = 15     # 新：TaogubaParser.ASSOC_MAX_GAP
_VALID = ("BUY", "SELL", "HOLD")


def collect(text):
    actions = [(m.start(), m.group(0)) for m in TaogubaParser.ACTION_RE.finditer(text)
               if ACTION_MAP.get(m.group(0)) in _VALID]
    names = nm.scan_names(text)
    return actions, names


def old_claims(text, names, actions, window=OLD_WINDOW):
    """旧：按名认领动作词（一个动作词可被多只股认领）。返回 action_pos -> [names]。"""
    out = collections.defaultdict(list)
    for name, code, ns, ne in names:
        near = nm.associate(text, ns, ne, actions, window=window)
        if near:
            out[near[0]].append(name)
    return out


def new_claims(text, names, actions, window=NEW_WINDOW):
    """新：按动作词认领最近的 1 只（1:1）。返回 action_pos -> [names]。"""
    out = collections.defaultdict(list)
    claimed = set()
    for apos, kw in actions:
        if ACTION_MAP.get(kw) not in _VALID or apos in claimed:
            continue
        cand = nm.associate_to_action(text, apos, kw, names, window=window)
        if cand:
            claimed.add(apos)
            out[apos].append(cand[0])
    return out


def summarize(text):
    actions, names = collect(text)
    old = old_claims(text, names, actions)
    new = new_claims(text, names, actions)
    old_1n = {p: n for p, n in old.items() if len(n) > 1}
    new_1n = {p: n for p, n in new.items() if len(n) > 1}
    return {
        "old_trades": sum(len(v) for v in old.values()),
        "new_trades": sum(len(v) for v in new.values()),
        "old_1n_actions": len(old_1n),
        "new_1n_actions": len(new_1n),
        "old_1n_examples": {f"@{p}": n for p, n in list(old_1n.items())[:4]},
    }


def main(want_authors=None):
    rows = []
    with open(CORPUS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("site") == "taoguba" and r.get("text_kind") == "full" and r.get("text"):
                rows.append(r)
    # 默认挑"旧规则产出最多股票"的若干帖（1:N 最重灾区）
    scored = []
    for r in rows:
        s = summarize(r["text"])
        scored.append((s["old_trades"] - s["new_trades"], s["old_1n_actions"], r, s))
    scored.sort(key=lambda x: (-x[0], -x[1]))

    if want_authors:
        sel = [x for x in scored if x[2]["nickname"] in set(want_authors)]
    else:
        sel = scored[:10]

    print(f"语料 taoguba/full 帖数={len(rows)}；展示 {len(sel)} 帖（旧→新 减少最多者）\n")
    tot_old = tot_new = 0
    for delta, o1n, r, s in sel:
        tot_old += s["old_trades"]
        tot_new += s["new_trades"]
        print("=" * 96)
        print(f"作者={r['nickname']}  {r['post_url']}")
        print(f"  旧规则④ 产出 {s['old_trades']} 条 / 其中 '一句多票'动作词 {s['old_1n_actions']} 个")
        print(f"  新规则④ 产出 {s['new_trades']} 条 / 其中 '一句多票'动作词 {s['new_1n_actions']} 个")
        if s["old_1n_examples"]:
            print(f"  旧：动作词→多只股 例：{s['old_1n_examples']}")
        print(f"  原文(截 260)：{(r['text'] or '')[:260]}")
    print("=" * 96)
    print(f"汇总（这 {len(sel)} 帖）：旧 {tot_old} 条 → 新 {tot_new} 条（-{tot_old-tot_new}）")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
