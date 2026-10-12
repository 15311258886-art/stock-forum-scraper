"""审查取证脚本：验证情绪打分子串误判、热度公式、first_seen 空串缺陷。只读/纯计算，不改库。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from overseas import lexicon as lx  # noqa: E402
from overseas.seed import SEED_NEWS  # noqa: E402

print("=== 1) 情绪词库子串误判（score_sentiment 未用 _count_kw 词边界）===")
samples = [
    "Apple executive says iPhone demand is strong",
    "Emissions target unveiled at climate summit",
    "Urban housing plan announced by ministry",
    "Company dismissed the report as inaccurate",
    "Nvidia rally continues as data center revenue beats",
    "Ukraine peace talks show progress, markets rebound",
]
for s in samples:
    lab, sc = lx.score_sentiment(s)
    print(f"  score={sc:+.2f} {lab:4s} | {s}")

print("\n  -- 误命中溯源：哪些词被 substring 命中 --")
for w in ("cut", "miss", "ban", "loss", "halt", "fall", "risk", "weak", "sell"):
    if w in lx.BEAR_EN:
        hits = [x for x in ("executive", "emissions", "urban", "abandon", "dismissed",
                            "glossary", "waterfall", "banner", "Bangladesh")
                if w in x]
        if hits:
            print(f"  BEAR '{w}' 会误命中: {hits}")

print("\n=== 2) 真实语料（seed 41 条）上的子串命中统计 ===")
tot_sub = tot_bound = 0
for it in SEED_NEWS:
    low = f"{it.title} {it.summary}".lower()
    for w in lx.BEAR_EN + lx.BULL_EN:
        tot_sub += low.count(w)
        if any("a" <= c <= "z" for c in w):
            tot_bound += len(lx._KW_RE_CACHE.setdefault(
                w, __import__("re").compile(r"(?<![a-z0-9])" + __import__("re").escape(w) + r"(?![a-z0-9])")
            ).findall(low))
        else:
            tot_bound += low.count(w)
print(f"  子串计数命中总数 = {tot_sub}；词边界命中总数 = {tot_bound}；"
      f"虚增 = {tot_sub - tot_bound} ({(tot_sub - tot_bound) / max(tot_bound,1) * 100:.1f}%)")

print("\n=== 3) heat 公式是否含真实热度/时间衰减 ===")
for it in SEED_NEWS[:3]:
    lab, sc = lx.score_sentiment(f"{it.title} {it.summary}")
    topic, topics = lx.classify_topic(f"{it.title} {it.summary}", {})
    heat = round((abs(sc) * 2.0 + len(topics) * 1.5 + 1.0) * 1.0 * 10, 2)
    print(f"  heat={heat:7.2f} | emotion={sc:+.2f} topics={len(topics)} | {it.title[:52]}")
print("  结论：heat 仅由 |情绪分| 与主题命中数决定，与阅读量/评论数/转发无关，且无时间衰减。")

print("\n=== 4) first_seen 空串 min() 缺陷 ===")
rows = [{"publish_time": "2026-09-20 10:00:00"}, {"publish_time": ""}, {"publish_time": "2026-09-18 08:00:00"}]
bad = min((r.get("publish_time") or "") for r in rows)
good = min((r.get("publish_time") or "") for r in rows if r.get("publish_time"))
print(f"  现有写法 min(...) = {bad!r}  → 空串最小，触发 or datetime.now()")
print(f"  正确写法 min(过滤后) = {good!r}")

print("\n=== 5) API 硬编码股票代码 ===")
import re  # noqa: E402
src = (ROOT / "viewer" / "server.py").read_text(encoding="utf-8")
m = re.search(r'for code in \((.*?)\):', src, re.S)
codes = re.findall(r'"(\d{6})"', m.group(1)) if m else []
print(f"  /api/person 提及股票仅匹配这 {len(codes)} 个硬编码代码: {codes}")
