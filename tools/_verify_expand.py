import sys, os
ROOT = r"f:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper"
sys.path.insert(0, os.path.join(ROOT, "viewer"))
sys.path.insert(0, ROOT)

import demo_data as dd
from scraper.players import select_top_players, label_channels, DEFAULT_CHANNEL_KEYWORDS

candidates = dd._build_candidates()
print("候选池(pool)规模:", len(candidates))

raw = [{"name": n, "rank_position": rk, "ranking_score": sc, "is_certified": cert,
        "tags": tags, "has_position": hp, "source_url": "https://example.local/demo"}
       for (n, rk, sc, cert, tags, hp, plat, fans, infl) in candidates]

sel_rules = {"top_n": 200, "top_pct": 1.0, "require_verifiable": True,
             "certify_auto_include": True, "min_score": None, "active_within_days": None}
chosen = label_channels(select_top_players(raw, sel_rules), [], DEFAULT_CHANNEL_KEYWORDS)
core_names = {p.name for p in chosen}

print("纳入测评与关注(入选):", len(chosen))
print("  认证选手数:", sum(1 for p in chosen if p.is_certified))
print("  渠道分布:", {c: sum(1 for p in chosen if p.channel == c) for c in "ABCD"})
print("  名次区间: min=%s max=%s" % (min(p.rank_position for p in chosen),
                                     max(p.rank_position for p in chosen)))

# 候选池是否落在 500~1000 区间
assert 500 <= len(candidates) <= 1000, "候选池未落在 500~1000 区间"
# 入选是否恰好 ~200（认证优先前提下收口到 200）
assert len(chosen) == 200, "入选人数不是 200，实际=%d" % len(chosen)
print("\n✅ 校验通过：候选池 %d 人（500~1000 区间），经原有筛选取前 200 人纳入测评与关注。" % len(candidates))
