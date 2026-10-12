# -*- coding: utf-8 -*-
"""探针：P1(post_time) + P2(脏名) 现状调查。只读，输出 tools/_p1p2_probe.log。"""
from __future__ import annotations
import json, collections, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
BASE = Path(r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据")
log = open(ROOT / "tools" / "_p1p2_probe.log", "w", encoding="utf-8")
sys.stdout = log

CORPUS = BASE / "players_posts.jsonl"
STAGING = BASE / "trades_v2_staging.jsonl"

# ---- 语料结构 ----
print("=" * 70)
print("语料 players_posts.jsonl 记录结构")
print("=" * 70)
keys = collections.Counter()
pt_samples = []
recs = []
for line in CORPUS.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    r = json.loads(line)
    recs.append(r)
    keys.update(r.keys())
    if len(pt_samples) < 5:
        pt_samples.append({k: (str(r.get(k))[:60]) for k in
                           ("site", "nickname", "post_id", "post_url", "publish_time")})
print("总行:", len(recs))
print("字段频次:", dict(keys))
print("样例:")
for s in pt_samples:
    print("  ", s)

# 语料 post_id 去重性 & publish_time 非空率
has_pid = [r for r in recs if r.get("post_id")]
has_pt = [r for r in recs if r.get("publish_time")]
print("\n有 post_id 字段的行 =", len(has_pid), "  有 publish_time 的行 =", len(has_pt))
print("publish_time 样例(前10):", [r.get("publish_time") for r in recs[:10]])

# ---- staging 现状 ----
print("\n" + "=" * 70)
print("staging trades_v2_staging.jsonl 现状")
print("=" * 70)
srows = [json.loads(l) for l in STAGING.read_text(encoding="utf-8").splitlines() if l.strip()]
print("行数:", len(srows))
pt = collections.Counter(r.get("post_time") for r in srows)
print("distinct post_time =", len(pt), " 取值:", pt.most_common(5))
authors = {r.get("author") for r in srows}
print("distinct author =", len(authors))
syms = collections.Counter(r.get("symbol") for r in srows)
print("distinct symbol =", len(syms))

# ---- P2：目标脏名行 ----
print("\n" + "=" * 70)
print("P2 脏名行（000688 / 000001 / 全角A）")
print("=" * 70)
targets = ("000688", "000001")
fullwidth = [r for r in srows if any(ch in (r.get("symbol_name") or "") for ch in "ＡＢＣ")]
print("含全角字母的 symbol_name 行 =", len(fullwidth))
for r in fullwidth[:8]:
    print("  - pid=%s site=%s sym=%s name=%r" % (r.get("post_id"), r.get("site"), r.get("symbol"), r.get("symbol_name")))
for s in targets:
    rows = [r for r in srows if r.get("symbol") == s]
    print("\n  symbol=%s 共 %d 行:" % (s, len(rows)))
    for r in rows:
        print("    - pid=%s site=%s action=%s name=%r" % (r.get("post_id"), r.get("site"), r.get("action"), r.get("symbol_name")))

# 关联率试算：staging.post_id 能否在语料里找到
print("\n" + "=" * 70)
print("post_id 关联率试算")
print("=" * 70)
url_set = {r.get("post_url") for r in recs}
lastseg = {str(r.get("post_url") or "").rsplit("/", 1)[-1] for r in recs}
pid_set = {r.get("post_id") for r in recs if r.get("post_id")}
s_pids = [r.get("post_id") for r in srows]
n_url = sum(1 for p in s_pids if p in url_set)
n_last = sum(1 for p in s_pids if p in lastseg)
n_pid = sum(1 for p in s_pids if p in pid_set)
print("staging.post_id 命中 语料.post_url =", n_url, "/", len(s_pids))
print("staging.post_id 命中 语料.post_url 尾段 =", n_last, "/", len(s_pids))
print("staging.post_id 命中 语料.post_id =", n_pid, "/", len(s_pids))
print("语料 post_url 唯一数:", len(url_set), " 语料 post_id 唯一数:", len(pid_set))
