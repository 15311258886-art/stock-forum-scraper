# -*- coding: utf-8 -*-
"""P2 r2 前后对比：被丢弃的行 + 新 staging 指标。只读。"""
from __future__ import annotations
import json, sys, collections
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
BASE = Path(r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据")
log = open(ROOT / "tools" / "_p2r2_diff.log", "w", encoding="utf-8")
sys.stdout = log


def load(p):
    return [json.loads(l) for l in Path(p).read_text(encoding="utf-8").splitlines() if l.strip()]


before = load(BASE / "_staging" / "trades_v2_p2_before.jsonl")
after = load(BASE / "trades_v2_staging.jsonl")


def key(r):
    return (r.get("post_id"), r.get("action"), r.get("symbol"))


bk = collections.Counter(key(r) for r in before)
ak = collections.Counter(key(r) for r in after)
dropped = []
for r in before:
    k = key(r)
    if ak.get(k, 0) < bk.get(k, 0):
        bk[k] -= 1
        dropped.append(r)

print("before 行数 =", len(before), " after 行数 =", len(after))
print("被丢弃行数 =", len(dropped))
print("=" * 70)
for r in dropped:
    print("pid=%s site=%s action=%s symbol=%s name=%r" %
          (r.get("post_id"), r.get("site"), r.get("action"), r.get("symbol"), r.get("symbol_name")))
    print("   原文片段:", (r.get("raw_text") or "")[:160].replace("\n", " "))

print("=" * 70)
print("新 staging 指标：")
print("  行数 =", len(after))
print("  distinct author =", len(set(r.get("author") for r in after)))
cnt = collections.Counter(r.get("symbol") for r in after)
print("  distinct symbol =", len(cnt))
tot = len(after) or 1
print("  Top5:")
for s, c in cnt.most_common(5):
    print("    %s  %d  %.1f%%" % (s, c, 100.0 * c / tot))
byact = collections.Counter(r.get("action") for r in after)
bysite = collections.Counter(r.get("site") for r in after)
print("  按动作:", dict(byact), " 按站点:", dict(bysite))
pt = set(r.get("post_time") for r in after)
print("  distinct post_time =", len(pt))
print("  仍含指数名的行 =", sum(1 for r in after if "指数" in (r.get("symbol_name") or "") or (r.get("symbol_name") in ("科创50", "科创100"))))
