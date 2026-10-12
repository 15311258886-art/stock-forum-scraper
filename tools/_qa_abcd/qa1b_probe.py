# -*- coding: utf-8 -*-
"""QA(Edward) 独立验证 · 第1项补：①裸代码召回缺口 ②rule③+④ 双认领(1动作词→2票)复盘。
"""
from __future__ import annotations
import json, sys, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from scraper.parsers.taoguba import TaogubaParser, ACTION_MAP  # noqa: E402
from scraper import name_match as nm  # noqa: E402

P = TaogubaParser({})
CORPUS = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\players_posts.jsonl"
STAGING = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\trades_v2_staging.jsonl"


def ext(t):
    return [(x["action"], x["symbol_name"], x["symbol"]) for x in P._extract_trades(t)]


print("#" * 100)
print("# 1) 裸代码 / 括号代码 召回对照")
print("#" * 100)
for t in ["卖出600519", "卖出 600519", "卖出（600519）", "卖出(600519)",
          "清仓宁德时代(300750)", "特发信息(000070)尾盘买进",
          "$澳弘电子(sh605058)$ 澳弘电子，B。", "买入贵州茅台"]:
    print(f"  {t!r:42s} -> {ext(t)}")

print()
print("#" * 100)
print("# 2) 全语料：单动作词产生 ≥2 票的帖（rule③ 前向 + rule④ 双认领）")
print("#" * 100)
rows = []
with open(CORPUS, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("site") == "taoguba" and r.get("text_kind") == "full" and r.get("text"):
            rows.append(r)


def action_claim_counts(text):
    """返回 {动作词位置: 认领到的名列表}，复刻 _extract_trades 的 ③ 与 ④ 认领轨迹。"""
    from scraper.parsers.taoguba import ACTION_MAP as AM
    # ① 代码锚定
    import re
    anchored = []
    claims = collections.defaultdict(list)
    for m in re.finditer(r"(?:\$)?([\u4e00-\u9fa5A-Za-z]{0,8})?[（(](?:sh|sz|SH|SZ)?(\d{6})[)）]", text):
        anchored.append((m.start(), m.end()))
        claims[("code", m.start())].append(m.group(2))
    consumed = list(anchored)
    # ③ 前向
    actions = []
    for m in P.ACTION_RE.finditer(text):
        kw = m.group(0)
        actions.append((m.start(), kw))
        if AM.get(kw, "") in ("BUY", "SELL", "HOLD"):
            fwd = nm.match_after(text, m.end(), max_len=6)
            if fwd:
                claims[("fwd", m.start())].append(fwd[0])
                consumed.append((fwd[2], fwd[3]))
    # ③ 后向
    for m in P.ACTION_RE.finditer(text):
        kw = m.group(0)
        if AM.get(kw, "") not in ("BUY", "SELL", "HOLD"):
            continue
        back = nm.match_ending(text, m.start(), max_len=8)
        if not back:
            continue
        if any(back[2] < c_end and back[3] > c_start for c_start, c_end in consumed):
            continue
        claims[("back", m.start())].append(back[0])
        consumed.append((back[2], back[3]))
    # ④
    names = nm.scan_names(text)

    def is_cons(ns, ne):
        return any(ns < ce and ne > cs for cs, ce in consumed)

    claimed_actions = set()
    for apos, kw in actions:
        if AM.get(kw, "") not in ("BUY", "SELL", "HOLD") or apos in claimed_actions:
            continue
        cand = nm.associate_to_action(text, apos, kw,
                                      [(n, c, s, e) for (n, c, s, e) in names if not is_cons(s, e)],
                                      window=P.ASSOC_MAX_GAP)
        if cand and not is_cons(cand[2], cand[3]):
            claimed_actions.add(apos)
            claims[("assoc", apos)].append(cand[0])
    return actions, claims


n_2plus = 0
examples = []
tot = 0
for r in rows:
    actions, claims = action_claim_counts(r["text"])
    tot += 1
    # 合并同一动作词的 前向/关联 认领
    by_pos = collections.defaultdict(list)
    for k, v in claims.items():
        if k[0] in ("fwd", "assoc", "back"):
            by_pos[k[1]].extend(v)
    bad = {p: v for p, v in by_pos.items() if len(set(v)) >= 2}
    if bad:
        n_2plus += 1
        if len(examples) < 6:
            examples.append((r["nickname"], r["post_url"], bad, r["text"]))

print(f"taoguba/full 帖数={tot}；其中「单个动作词认领到 ≥2 只股」的帖数={n_2plus} ({n_2plus/tot*100:.1f}%)")
for nick, url, bad, tx in examples:
    print("-" * 90)
    print(f"作者={nick} {url}")
    for pos, names in list(bad.items())[:3]:
        print(f"    动作词@{pos} -> {names}")
    print(f"    原文(截160): {tx[:160]}")

print()
print("#" * 100)
print("# 3) 新 staging 产出 分布（Top 股票 + 作者）")
print("#" * 100)
try:
    st_rows = []
    with open(STAGING, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                st_rows.append(json.loads(line))
    print("staging 行数:", len(st_rows))
    print("样例:", json.dumps(st_rows[0], ensure_ascii=False)[:300] if st_rows else "(空)")
    bysym = collections.Counter((r.get("symbol_name") or r.get("symbol")) for r in st_rows)
    print("Top12 标的:", bysym.most_common(12))
    bysite = collections.Counter(r.get("site") for r in st_rows)
    print("按站点:", bysite.most_common())
except Exception as e:
    print("staging 读取失败:", e)
