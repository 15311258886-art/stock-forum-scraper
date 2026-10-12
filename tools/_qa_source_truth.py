# -*- coding: utf-8 -*-
"""数据源接入状态自查：把「面板申报的状态」与「数据库里实际有什么」逐条对账。

只读，不写任何数据。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "viewer" / "app.db"
out = []


def p(s=""):
    out.append(str(s))


conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

# ---------- 0. 上次采集时间 ----------
p("=" * 72)
p("0) 元信息：上次采集时间")
p("=" * 72)
for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE '%overseas%' OR key LIKE '%collect%'"):
    p(f"  {r['key']} = {r['value']}")

# ---------- 1. 面板申报的状态 ----------
p()
p("=" * 72)
p("1) 面板数据源（overseas_sources_status）—— 界面直接读这张表")
p("=" * 72)
rows = list(conn.execute(
    "SELECT * FROM overseas_sources_status ORDER BY region, name"))
p(f"  共 {len(rows)} 行")
p(f"  {'key':<22}{'name':<26}{'region':<9}{'status':<22}{'ok':<4}{'count':<7}{'reqVPN':<7}{'checked_at'}")
for r in rows:
    p(f"  {str(r['source_key'])[:21]:<22}{str(r['name'])[:25]:<26}{str(r['region'])[:8]:<9}"
      f"{str(r['status'])[:21]:<22}{r['ok']:<4}{r['count']:<7}{r['requires_vpn']:<7}{r['checked_at']}")

# ---------- 2. 实际新闻里各源条目数 ----------
p()
p("=" * 72)
p("2) 数据库里实际的海外新闻条目（按 source_key 汇总）")
p("=" * 72)
news_by_key = {}
for r in conn.execute(
        "SELECT source_key, source_name, COUNT(*) n FROM overseas_news "
        "GROUP BY source_key, source_name ORDER BY n DESC"):
    news_by_key[r["source_key"]] = {"name": r["source_name"], "n": r["n"]}
p(f"  overseas_news 总计 {conn.execute('SELECT COUNT(*) c FROM overseas_news').fetchone()['c']} 条，"
  f"分属 {len(news_by_key)} 个 source_key")
for k, v in sorted(news_by_key.items(), key=lambda x: -x[1]["n"]):
    p(f"  {str(k)[:24]:<26}{str(v['name'])[:34]:<36}{v['n']} 条")

# ---------- 3. 对账：申报 vs 实际 ----------
p()
p("=" * 72)
p("3) 对账：申报『已接入/待激活/未启用』与实际是否有条目")
p("=" * 72)
contradictions = []
for r in rows:
    k = r["source_key"]
    st = r["status"] or ""
    n = news_by_key.get(k, {}).get("n", 0)
    if st.startswith("已接入") and n == 0:
        contradictions.append(("申报已接入但库里0条", k, r["name"], st, n))
    if (st.startswith("待激活") or st == "未启用" or st.startswith("抓取失败")) and n > 0:
        contradictions.append(("申报不可用但库里有条目", k, r["name"], st, n))
if contradictions:
    for c in contradictions:
        p(f"  [!] {c[0]}：{c[1]} / {c[2]} / 状态={c[3]} / 库内条目={c[4]}")
else:
    p("  未发现矛盾（申报状态与库内条目一致）")

# ---------- 4. 申报状态与 count 字段是否自洽 ----------
p()
p("=" * 72)
p("4) 申报状态里的『N条』与 count 字段、库内实际条数三方对照")
p("=" * 72)
bad3 = 0
for r in rows:
    k = r["source_key"]
    if (r["status"] or "").startswith("已接入"):
        n = news_by_key.get(k, {}).get("n", 0)
        if int(r["count"] or 0) != n:
            bad3 += 1
            p(f"  [!] {r['name']}：状态申报 {r['count']} 条（面板显示），库内实际 {n} 条  → 差 {int(r['count'] or 0) - n}")
p(f"  『已接入』源中，面板条数≠库内条数的：{bad3} 个"
  f"（差异来源：库内还额外混入了人工示例条目；或源被重复计入）")

# ---------- 5. 示例（seed）条目占比 ----------
p()
p("=" * 72)
p("5) 示例/示范条目混入情况")
p("=" * 72)
try:
    from overseas.seed import SEED_NEWS
    seed_pairs = {(i.title[:300], i.source_key) for i in SEED_NEWS}
    p(f"  overseas/seed.py 中 SEED_NEWS 条目数 = {len(SEED_NEWS)}")
    hit = 0
    by_key = {}
    for r in conn.execute("SELECT source_key, title FROM overseas_news"):
        if (r["title"], r["source_key"]) in seed_pairs:
            hit += 1
            by_key[r["source_key"]] = by_key.get(r["source_key"], 0) + 1
    p(f"  库内命中示例条目的条数 = {hit}")
    for k, v in sorted(by_key.items(), key=lambda x: -x[1]):
        p(f"      {k}: {v} 条")
    p()
    p("  SEED_NEWS 覆盖的 source_key 清单（这些源即使抓取失败，库里也会有条目）：")
    p("      " + ", ".join(sorted({i.source_key for i in SEED_NEWS})))
except Exception as e:
    p(f"  读取 seed 失败：{e!r}")

# ---------- 6. publish_time 被填成当天的情况 ----------
p()
p("=" * 72)
p("6) 时间字段：有多少条 publish_time 是「抓取当天」（疑似缺失后被填成 now）")
p("=" * 72)
for r in conn.execute(
        "SELECT substr(publish_time,1,10) d, COUNT(*) n FROM overseas_news "
        "GROUP BY d ORDER BY d DESC LIMIT 12"):
    p(f"  {r['d']}: {r['n']} 条")

# ---------- 7. 配置侧：enabled / requires_vpn / requires_key 对照 ----------
p()
p("=" * 72)
p("7) 配置侧真实开关 vs 面板状态（重点看『未启用』是否被误显示成『待激活』）")
p("=" * 72)
try:
    from overseas.sources import load_overseas_config, sources_of
    data = load_overseas_config()
    cfg_by_key = {s.get("key"): s for s in sources_of(data, enabled_only=False)}
    st_by_key = {r["source_key"]: r for r in rows}
    p(f"  配置文件源数 = {len(cfg_by_key)}；面板表行数 = {len(st_by_key)}")

    only_cfg = set(cfg_by_key) - set(st_by_key)
    only_db = set(st_by_key) - set(cfg_by_key)
    if only_cfg:
        p(f"  [!] 在配置里、面板表里没有（从未采集过）：{sorted(only_cfg)}")
    if only_db:
        p(f"  [!] 只在面板表里、配置里已不存在（陈旧残留行）：{sorted(only_db)}")

    p()
    p("  逐条：配置 enabled / requires_vpn / requires_key  →  面板 status")
    mis = 0
    for k in sorted(cfg_by_key):
        c = cfg_by_key[k]
        st = st_by_key.get(k)
        en = bool(c.get("enabled"))
        rv = bool(c.get("requires_vpn"))
        rk = bool(c.get("requires_key"))
        s = (st["status"] if st else "（无记录）")
        flag = ""
        if (not en) and (rv or rk) and str(s).startswith("待激活"):
            flag = "   <== 关源却被显示为『待激活』"
            mis += 1
        if en and not st:
            flag = flag or "   <== 已启用但无采集记录"
        p(f"    {k:<22} enabled={str(en):<6} vpn={str(rv):<6} key={str(rk):<6} -> {s}{flag}")
    p()
    p(f"  合计『已关闭却被显示为待激活』的源 = {mis} 个")
except Exception as e:
    p(f"  读配置失败：{e!r}")

txt = "\n".join(out)
with open(ROOT / "tools" / "_qa_source_truth.txt", "w", encoding="utf-8") as f:
    f.write(txt + "\n")
print("written tools/_qa_source_truth.txt")
