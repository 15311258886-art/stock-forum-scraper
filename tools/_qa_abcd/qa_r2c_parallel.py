# -*- coding: utf-8 -*-
"""QA(Edward) 只读取证：①「并列多标的」触发形态 vs ② 1a 受影响帖 —— 求交集 + 找可验证判据。
不修改任何代码。"""
from __future__ import annotations
import json, sys, collections
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from scraper.parsers.taoguba import TaogubaParser, ACTION_MAP  # noqa: E402
from scraper import name_match as nm  # noqa: E402

P = TaogubaParser({})
CORPUS = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\players_posts.jsonl"
OUT = ROOT / "tools" / "_qa_abcd" / "out"
VALID = ("BUY", "SELL", "HOLD")
SEPS = ("、", "/", "／", "和")

rows = []
with open(CORPUS, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("site") == "taoguba" and r.get("text_kind") == "full" and r.get("text"):
            rows.append(r)


def pid(r):
    return (r.get("post_url") or "").rsplit("/", 1)[-1]


def actions_of(text):
    return [(m.start(), m.group(0)) for m in P.ACTION_RE.finditer(text)
            if ACTION_MAP.get(m.group(0), "") in VALID]


# ---- Set B: 1a 受影响帖（旧逻辑下「单动作词认领≥2只」）----
def old_extra_multi(text):
    """旧（未修）逻辑：规则④ 不排除规则③已用动作词 → 单动作词可认领≥2只。返回是否发生。"""
    actions = actions_of(text)
    consumed = []
    claims = collections.defaultdict(list)
    # 规则③ 前向
    for apos, kw in actions:
        fwd = nm.match_after(text, apos + len(kw), max_len=6)
        if fwd:
            claims[apos].append(fwd[0])
            consumed.append((fwd[2], fwd[3]))
    # 规则③ 后向
    for apos, kw in actions:
        back = nm.match_ending(text, apos, max_len=8)
        if not back:
            continue
        if any(back[2] < ce and back[3] > cs for cs, ce in consumed):
            continue
        claims[apos].append(back[0])
        consumed.append((back[2], back[3]))
    # 规则④（旧：claimed_actions 不预置规则③已用动作词）
    names = nm.scan_names(text)

    def is_cons(ns, ne):
        return any(ns < ce and ne > cs for cs, ce in consumed)

    claimed = set()
    for apos, kw in actions:
        if apos in claimed:
            continue
        cand = nm.associate_to_action(text, apos, kw,
                                      [(n, c, s, e) for (n, c, s, e) in names if not is_cons(s, e)],
                                      window=P.ASSOC_MAX_GAP)
        if cand and not is_cons(cand[2], cand[3]):
            claimed.add(apos)
            claims[apos].append(cand[0])
    bad = {p: v for p, v in claims.items() if len(set(v)) >= 2}
    return bad


# ---- Set A: 「并列多标的」触发形态（我建议的补丁会命中的形态）----
def tight_parallels(text):
    """动作词后：名 + 紧邻分隔符(、/／和) + 另一只名（两者都是词典真名）。返回 [(动作, 名1, 名2, 片段)]。"""
    out = []
    for apos, kw in actions_of(text):
        fwd = nm.match_after(text, apos + len(kw), max_len=6)
        if not fwd:
            continue
        n1, c1, ns1, ne1 = fwd
        # 跳过分隔符（允许零个空格）
        j = ne1
        while j < len(text) and text[j] in " \u3000":
            j += 1
        if j >= len(text) or text[j] not in SEPS:
            continue
        sep = text[j]
        k = j + 1
        while k < len(text) and text[k] in " \u3000":
            k += 1
        fwd2 = nm.match_after(text, k, max_len=6)
        if not fwd2 or fwd2[0] == n1:
            continue
        out.append((kw, n1, fwd2[0], sep, text[max(0, apos - 4):fwd2[3] + 2]))
    return out


A = {}   # pid -> (row, parallels)
B = {}   # pid -> (row, bad)
for r in rows:
    tp = tight_parallels(r["text"])
    if tp:
        A[pid(r)] = (r, tp)
    be = old_extra_multi(r["text"])
    if be:
        B[pid(r)] = (r, be)

inter = set(A) & set(B)
print(f"总 taoguba/full 帖数 = {len(rows)}")
print(f"Set A（并列触发形态：动作词后 名+紧邻分隔符+名）= {len(A)} 帖")
print(f"Set B（1a 受影响：旧逻辑单动作词认领≥2只）        = {len(B)} 帖")
print(f"交集 A∩B = {len(inter)} 帖")
print(f"A 是否 ⊆ B ? {set(A) <= set(B)}   (A\\B = {len(set(A) - set(B))} 帖)")
print()

print("=" * 100)
print("Set A 全部（完整 post_id + 触发片段 + 原文）")
print("=" * 100)
with open(OUT / "r2c_setA.txt", "w", encoding="utf-8") as f:
    for p, (r, tp) in A.items():
        f.write(f"--- post_id={p}  author={r['nickname']}  url={r['post_url']}\n")
        for kw, n1, n2, sep, seg in tp:
            f.write(f"    触发: 动作[{kw}] 名[{n1}] sep[{sep}] 名[{n2}]  片段=…{seg}…\n")
        f.write(f"    原文: {r['text']}\n\n")
print(f"  已写入 {OUT/'r2c_setA.txt'}（{len(A)} 帖全文）")

print()
print("=" * 100)
print("Set B 全部 post_id 列表")
print("=" * 100)
with open(OUT / "r2c_setB.txt", "w", encoding="utf-8") as f:
    for p, (r, be) in B.items():
        f.write(f"{p}\t{r['nickname']}\t{ {pos: v for pos, v in be.items()} }\n")
print(f"  已写入 {OUT/'r2c_setB.txt'}（{len(B)} 帖 id 清单）")

print()
print("交集明细（是 1a 形态、也是我补丁触发形态 → 补丁会重造 1a 的假阳性）:")
with open(OUT / "r2c_intersection.txt", "w", encoding="utf-8") as f:
    for p in sorted(inter):
        r, tp = A[p]
        f.write(f"--- post_id={p} author={r['nickname']}\n")
        for kw, n1, n2, sep, seg in tp:
            f.write(f"    动作[{kw}] 名[{n1}]{sep}名[{n2}]  片段=…{seg}…\n")
    print(f"  交集 {len(inter)} 帖已写入 {OUT/'r2c_intersection.txt'}")

print()
print("=" * 100)
print("判据探查：真并列 vs 复盘列举 的可验证差异")
print("=" * 100)
# 对 A 帖，统计触发时并列名个数（分隔符链长度）与是否含"复盘/列举"语境词
CTX = ("复盘", "梯队", "断板", "涨停", "板", "明天", "明日", "关注", "看点", "题材", "介入")
for p, (r, tp) in A.items():
    kw, n1, n2, sep, seg = tp[0]
    ctx = [w for w in CTX if w in seg]
    print(f"  {p:12s} {r['nickname']:14s} 动作[{kw}]{n1}{sep}{n2}  列举语境词={ctx}")
