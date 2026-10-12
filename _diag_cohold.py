# -*- coding: utf-8 -*-
"""回查「共同买入/卖出」面板是否失真：按 条目数 vs 去重作者数 两口径拆解。"""
import sqlite3, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DB = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\viewer\app.db"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
c = con.cursor()

def q(sql, *a):
    return c.execute(sql, a).fetchall()

print("=" * 70)
print("【1】trades 总览")
print("=" * 70)
tot = q("SELECT COUNT(*) n FROM trades")[0]["n"]
sym = q("SELECT COUNT(DISTINCT symbol) n FROM trades WHERE symbol<>''")[0]["n"]
au = q("SELECT COUNT(DISTINCT author) n FROM trades WHERE author<>''")[0]["n"]
print(f"总条目={tot}  去重标的总数={sym}  去重作者数={au}")

print()
print("  按 action 分布：")
for r in q("SELECT action, COUNT(*) n, COUNT(DISTINCT author) a, COUNT(DISTINCT symbol) s FROM trades GROUP BY action ORDER BY n DESC"):
    print(f"    {r['action']:<8} 条目={r['n']:<5} 作者={r['a']:<5} 标的={r['s']}")

print()
print("  按 site 分布：")
for r in q("SELECT site, COUNT(*) n, COUNT(DISTINCT author) a FROM trades GROUP BY site ORDER BY n DESC"):
    print(f"    {str(r['site']):<12} 条目={r['n']:<5} 作者={r['a']}")

print()
print("  时间列填充率（决定 cohold_recent 能不能取到）：")
for col in ("buy_time", "sell_time", "post_time"):
    r = q(f"SELECT SUM(CASE WHEN COALESCE({col},'')<>'' THEN 1 ELSE 0 END) f, COUNT(*) n FROM trades")[0]
    print(f"    {col:<10} 非空={r['f']}/{r['n']}")

print()
print("=" * 70)
print("【2】Top 15 标的：条目数 vs 去重作者数（关键：是否真人多，还是同几个人刷条目）")
print("=" * 70)
rows = q("""SELECT symbol, symbol_name, COUNT(*) entries, COUNT(DISTINCT author) authors
            FROM trades WHERE symbol<>'' AND author<>''
            GROUP BY symbol ORDER BY entries DESC LIMIT 15""")
print(f"  {'代码':<8}{'名称':<12}{'条目':>6}{'作者':>6}{'条目/作者':>10}")
for r in rows:
    ratio = r["entries"] / max(1, r["authors"])
    print(f"  {r['symbol']:<8}{str(r['symbol_name'] or ''):<12}{r['entries']:>6}{r['authors']:>6}{ratio:>10.2f}")

print()
print("=" * 70)
print("【3】Top 15 标的：按去重作者数排序（这才是「共同持有」真正的口径）")
print("=" * 70)
rows = q("""SELECT symbol, symbol_name, COUNT(DISTINCT author) authors, COUNT(*) entries
            FROM trades WHERE symbol<>'' AND author<>''
            GROUP BY symbol ORDER BY authors DESC LIMIT 15""")
for r in rows:
    print(f"  {r['symbol']:<8}{str(r['symbol_name'] or ''):<12} 作者={r['authors']:>4}  条目={r['entries']}")

print()
print("=" * 70)
print("【4】与 players 表(入围选手池)的交集：这 200+ 选手有多少人真的在 trades 里")
print("=" * 70)
try:
    pl = q("SELECT COUNT(*) n FROM players")[0]["n"]
    print(f"  players 表人数={pl}")
    inter = q("""SELECT COUNT(DISTINCT t.author) n FROM trades t
                 WHERE t.author IN (SELECT name FROM players)""")[0]["n"]
    print(f"  trades 作者命中 players 的人数={inter}")
except Exception as e:
    print("  players 查询失败:", e)

print()
print("=" * 70)
print("【5】抽查 Top4 标的的原始文本（判断是否为「顺嘴一提」被当成买卖）")
print("=" * 70)
for sym, nm in (("300750", "宁德时代"), ("600519", "茅台"), ("601318", "中国平安"), ("000001", "平安银行"), ("600663", "陆家嘴")):
    rs = q("""SELECT author, action, post_time, substr(COALESCE(raw_text,''),1,120) t
              FROM trades WHERE symbol=? ORDER BY id DESC LIMIT 6""", sym)
    print(f"\n  ── {nm}({sym}) 共 {q('SELECT COUNT(*) n FROM trades WHERE symbol=?', sym)[0]['n']} 条，最近 6 条：")
    for r in rs:
        txt = (r["t"] or "").replace("\n", " ")
        print(f"     [{r['action']:<4}] {str(r['author'])[:10]:<10} {str(r['post_time'] or '')[:16]} | {txt}")

print()
print("=" * 70)
print("【6】面板真实输出 cohold_recent(5) 的 top 列表")
print("=" * 70)
sys.path.insert(0, r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\viewer")
try:
    from db import DB as DBCls  # noqa
except Exception:
    DBCls = None
if DBCls:
    try:
        d = DBCls(DB)
        for act in ("buy", "sell"):
            res = d.cohold_recent(days=5)
            key = res[act]
            print(f"\n  最近5日 共同{('买入' if act=='buy' else '卖出')}：anchor={res['anchor']}({res['anchor_source']}) 窗口={res['window_start']}~{res['window_end']}")
            if not key:
                print("    （空）")
            for g in key[:8]:
                print(f"    {g['label']:<24} 人数={g['player_count']:<4} 首={g['first_time'][:10]} 末={g['last_time'][:10]}")
    except Exception as e:
        import traceback; traceback.print_exc()
else:
    print("  未能导入 db 模块，跳过")
con.close()
