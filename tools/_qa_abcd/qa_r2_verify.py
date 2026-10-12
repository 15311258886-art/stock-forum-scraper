# -*- coding: utf-8 -*-
"""QA(Edward) 第二轮复验：4 组 API 的机器号命中统计 + C 回归。"""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from viewer.db import is_bot_author as viewer_is_bot          # noqa: E402
from scraper.bot_filter import is_bot_author as scraper_is_bot  # noqa: E402

OUT = ROOT / "tools" / "_qa_abcd" / "out"


def load(n):
    return json.load(open(OUT / n, encoding="utf-8"))


def consensus_authors(data):
    s = {}
    for x in data:
        for p in x.get("players") or []:
            s[p["player"]] = x.get("symbol_name")
    return s


def cohold_authors(data):
    s = {}
    for grp in ("buy", "sell", "hold"):
        for x in data.get(grp) or []:
            for p in x.get("players") or []:
                s[p["player"]] = x.get("symbol_name")
    return s


def report(tag, authors):
    vb = sorted(n for n in authors if viewer_is_bot(n))
    sb = sorted(n for n in authors if scraper_is_bot(n)[0])
    print(f"  [{tag}] authors={len(authors)}  viewer_bot={len(vb)}  scraper_bot={len(sb)}")
    if vb:
        print(f"      viewer_bot 命中: {vb}")
    if sb:
        print(f"      scraper_bot 命中: {sb}")
    return len(vb), len(sb), vb


print("=" * 90)
print("A) 四组 API 机器号统计（viewer 判定 / scraper 判定）")
print("=" * 90)
c = load("r2_consensus.json")
co = load("r2_consensus_oq.json")
h = load("r2_cohold.json")
ho = load("r2_cohold_oq.json")
r1 = report("consensus 默认", consensus_authors(c))
r2 = report("consensus only_qualified=1", consensus_authors(co))
r3 = report("cohold 默认", cohold_authors(h))
r4 = report("cohold only_qualified=1", cohold_authors(ho))

print()
print("=" * 90)
print("B) C 回归：buy/hold 分列仍在")
print("=" * 90)
print("  consensus 条数:", len(c), " oq=1 条数:", len(co))
for x in c[:6]:
    print(f"    {x['symbol_name']:<8}{x['symbol']}  总={x['holders']:>2} 买={x['buy_holders']:>2} 持={x['hold_holders']:>2} 关注={x['watch_holders']:>2}")
for tgt in ["平安银行", "中国平安", "宁德时代", "贵州茅台"]:
    e = [x for x in c if x["symbol_name"] == tgt]
    if e:
        x = e[0]
        print(f"    >> {tgt}: 总={x['holders']} 买={x['buy_holders']} 持={x['hold_holders']} 关注={x['watch_holders']}")
print("  cohold 组条数:", {k: len(h.get(k) or []) for k in ("buy", "sell", "hold")},
      " oq=1:", {k: len(ho.get(k) or []) for k in ("buy", "sell", "hold")})
print("  oq=1 consensus 全部行:")
for x in co:
    print(f"    {x['symbol_name']} 总={x['holders']} 买={x['buy_holders']} 持={x['hold_holders']} 选手={[p['player'] for p in x.get('players') or []]}")

print()
print("=" * 90)
print("C) 汇总判定")
print("=" * 90)
tot_viewer = r1[0] + r2[0] + r3[0] + r4[0]
tot_scraper = r1[1] + r2[1] + r3[1] + r4[1]
print(f"  四组 viewer_bot 合计 = {tot_viewer}  -> {'PASS(全 0)' if tot_viewer == 0 else 'FAIL'}")
print(f"  四组 scraper_bot 合计 = {tot_scraper}")
