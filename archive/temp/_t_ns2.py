# -*- coding: utf-8 -*-
"""复测风险隔离后的 api_niusan，并确认家族/实控人已被挡住。"""
import os, sys, json, sqlite3

BASE = os.getcwd()
sys.path.insert(0, os.path.join(BASE, "viewer"))
os.chdir(os.path.join(BASE, "viewer"))
import server as S

print("=" * 60)
print("[1] 牛散接口（风险隔离后）")
print("=" * 60)
d = S.api_niusan(min_hold=2, scope="free")
s = d.get("summary") or {}
print("summary:", json.dumps(s, ensure_ascii=False))
print()
print("牛散榜 Top12：")
for p in (d.get("investors") or [])[:12]:
    names = "/".join([(x.get("symbol_name") or "")[:6] for x in (p.get("holdings") or [])[:4]])
    print("  %-10s 持%2s只 合计%.2f%% 新进%s/减持%s | %s" % (
        p["name"][:10], p["stock_count"], p.get("total_ratio") or 0,
        p.get("new_cnt"), p.get("cut_cnt"), names))
print()
print("协同股（牛散同持）Top8：")
for c in (d.get("cohold") or [])[:8]:
    print("  %s %-8s %s位同持 合计%.2f%% | %s" % (
        c["symbol"], (c.get("symbol_name") or "")[:8], c.get("people_count"),
        c.get("star_ratio_sum") or 0, "/".join([x["name"] for x in (c.get("people") or [])[:5]])))

print()
print("=" * 60)
print("[2] 污染验证：家族持股是否已挡在牛散榜外")
print("=" * 60)
con = sqlite3.connect("file:app.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
# 已知污染样本：天富龙（朱大庆家族）、国芳集团（张国芳家族）、侨银股份
for sym, nm in [("603406", "天富龙"), ("601086", "国芳集团"), ("002973", "侨银股份")]:
    rows = con.execute(
        "SELECT holder_name, holder_kind, hold_ratio, holder_type FROM stock_holders "
        "WHERE symbol=? AND scope='free' ORDER BY holder_rank LIMIT 6", (sym,)).fetchall()
    if not rows:
        print("  %s %s: 未抓取" % (sym, nm))
        continue
    kinds = set((r["holder_kind"] or "") for r in rows if r["holder_type"] == "自然人")
    print("  %s %s: 自然人标签=%s  样例=%s" % (
        sym, nm, sorted(kinds), [(r["holder_name"], r["holder_kind"]) for r in rows[:3]]))
print()
print("=== 全库 holder_kind 分布 ===")
for r in con.execute("SELECT holder_kind, COUNT(*) n, COUNT(DISTINCT holder_name) ppl "
                     "FROM stock_holders GROUP BY holder_kind ORDER BY n DESC"):
    print("  %-14s 记录%-7s 人数%s" % (r["holder_kind"], r["n"], r["ppl"]))
con.close()
