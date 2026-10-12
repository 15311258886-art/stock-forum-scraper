# -*- coding: utf-8 -*-
"""Inspect bot-author coverage in the live DB (read-only)."""
import re
import sqlite3

conn = sqlite3.connect("viewer/app.db")
conn.row_factory = sqlite3.Row
L = []


def out(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    L.append(s)


BOT_NAMES = {"神光财经", "Sina财经产", "新浪财经客", "新浪财经芝", "看财经官博",
             "汽车营销分", "机工战略", "EC品牌观察"}
_PLATFORM = (r"新浪|Sina|sina|腾讯|网易|搜狐|凤凰|和讯|财联社|证券之星|金融界|"
             r"同花顺|东方财富|格隆汇|新华|中金在线|每日经济|第一财经|界面|澎湃|"
             r"平安银行|招商银行|中金|雪球|证券时报|上海证券报|中国证券报")
_SUFFIX = (r"资讯|播报|快讯|日报|晚报|早报|午报|公告|情报|速递|时报|周刊|"
           r"看盘|解盘|盘点|官博|官微|情报站")
_BRANDS = r"神光|益盟|大智慧|指南针|雪球财经|同花顺财经|东方财富网"
PATS = [re.compile(rf"^(?:{_PLATFORM}).{{0,8}}(?:{_SUFFIX}|财经|观察|视点|焦点)$"),
        re.compile(rf"^.{{0,6}}(?:{_BRANDS}).{{0,6}}$"),
        re.compile(rf"^.{{0,8}}(?:{_SUFFIX})$")]


def is_bot(name):
    n = (name or "").strip()
    if not n:
        return False, ""
    if n in BOT_NAMES:
        return True, "NAMES"
    for rx in PATS:
        if rx.search(n):
            return True, "PAT:" + rx.pattern[:40]
    return False, ""


out("== players.axis distribution ==")
for r in conn.execute("SELECT COALESCE(axis,'(null)') axis, COUNT(*) n FROM players GROUP BY axis"):
    out("  ", dict(r))

out("")
out("== Tier A players: axis + bot judgement ==")
for r in conn.execute("SELECT name, axis FROM players WHERE tier='A' ORDER BY name"):
    b, why = is_bot(r["name"])
    out("   %-16s axis=%-6s bot=%s %s" % (r["name"], r["axis"], b, why))

out("")
out("== distinct author names appearing in trades / positions, with bot judgement ==")
names = set()
for r in conn.execute("SELECT DISTINCT author AS n FROM trades WHERE author IS NOT NULL AND author<>''"):
    names.add(r["n"])
for r in conn.execute("SELECT DISTINCT player AS n FROM positions WHERE player IS NOT NULL AND player<>''"):
    names.add(r["n"])
bots = []
for n in sorted(names):
    b, why = is_bot(n)
    if b:
        bots.append((n, why))
out("   total distinct authors:", len(names), "| judged bot:", len(bots))
for n, why in bots:
    out("   BOT  %-18s %s" % (n, why))

out("")
out("== of those bots, how many have players.axis set? ==")
for n, why in bots:
    row = conn.execute("SELECT tier, axis FROM players WHERE name=?", (n,)).fetchone()
    out("   %-18s tier=%s axis=%s" % (n, (row["tier"] if row else "-"), (row["axis"] if row else "-")))

with open("tools/_holdfix/botscan.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(L))
