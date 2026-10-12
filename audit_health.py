# -*- coding: utf-8 -*-
"""整体体检：及时性 / 完整性 / 疏漏 三合一审计（只读）。"""
import os, sqlite3, json, datetime, re

BASE = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper"
DB = os.path.join(BASE, "viewer", "app.db")
OUT = os.path.join(BASE, "_audit_all_out.txt")
lines = []
def P(*a):
    s = " ".join(str(x) for x in a)
    lines.append(s)
    print(s)

now = datetime.datetime.now()
P("审计时间:", now.strftime("%Y-%m-%d %H:%M:%S"))
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()

# ---- 0. 全表与行数 ----
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tabs = [r[0] for r in cur.fetchall()]
P("\n===== 0. 表清单与行数 =====")
for t in tabs:
    try:
        cur.execute(f'SELECT COUNT(*) FROM "{t}"')
        P(f"  {cur.fetchone()[0]:>7}  {t}")
    except Exception as e:
        P(f"  ERR {t}: {e}")

def cols(t):
    cur.execute(f'PRAGMA table_info("{t}")')
    return [r[1] for r in cur.fetchall()]

# ---- 1. 及时性：各表时间列最新值 ----
P("\n===== 1. 及时性（数据新鲜度） =====")
time_cols = {
    "positions": ["post_time", "created_at", "updated_at", "fetched_at"],
    "trades": ["post_time", "created_at", "updated_at", "fetched_at"],
    "news": ["post_time", "created_at", "updated_at", "pub_time", "fetched_at"],
    "posts": ["post_time", "created_at", "updated_at", "fetched_at"],
    "players": ["updated_at", "created_at", "last_seen"],
    "raw_pages": ["fetched_at", "created_at", "updated_at"],
    "lhb_stocks": ["trade_date", "created_at", "fetched_at"],
    "lhb_seats": ["trade_date", "created_at", "fetched_at"],
}
for t, cand in time_cols.items():
    if t not in tabs: continue
    cs = cols(t)
    usable = [c for c in cand if c in cs]
    if not usable:
        P(f"  {t}: 无时间列（可用列: {cs}）")
        continue
    for c in usable[:2]:
        try:
            cur.execute(f'SELECT MAX("{c}") FROM "{t}"')
            mx = cur.fetchone()[0]
            P(f"  {t}.{c} 最新 = {mx}")
        except Exception as e:
            P(f"  {t}.{c} ERR {e}")

# ---- 2. 完整性：字段空值率 ----
P("\n===== 2. 完整性（关键字段空值率） =====")
checks = [
    ("positions", ["symbol", "symbol_name", "ratio", "post_url", "site", "player"]),
    ("trades", ["symbol", "symbol_name", "action", "site", "author"]),
    ("players", ["name", "site", "channel", "source_url", "tier", "real_holding"]),
    ("news", ["title", "url", "site"]),
    ("posts", ["content", "url", "site"]),
]
for t, cs in checks:
    if t not in tabs: continue
    have = cols(t)
    total = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    if not total:
        P(f"  {t}: 空表"); continue
    P(f"  --- {t}（{total} 行）---")
    for c in cs:
        if c not in have:
            P(f"      {c}: 列不存在")
            continue
        n = cur.execute(f'SELECT COUNT(*) FROM "{t}" WHERE "{c}" IS NULL OR TRIM(CAST("{c}" AS TEXT))=\'\'').fetchone()[0]
        P(f"      {c}: 空 {n}/{total} ({100*n/total:.0f}%)")

# ---- 3. 来源覆盖 ----
P("\n===== 3. 来源覆盖（site / channel 分布） =====")
for t in ["players", "positions", "trades", "news", "posts"]:
    if t not in tabs: continue
    have = cols(t)
    if "site" not in have: continue
    cur.execute(f'SELECT site, COUNT(*) FROM "{t}" GROUP BY site ORDER BY 2 DESC')
    P(f"  {t}: " + ", ".join(f"{r[0] or '(空)'}={r[1]}" for r in cur.fetchall()))
if "players" in tabs and "channel" in cols("players"):
    cur.execute("SELECT channel, COUNT(*) FROM players GROUP BY channel ORDER BY 2 DESC")
    P("  players.channel: " + ", ".join(f"{r[0] or '(空)'}={r[1]}" for r in cur.fetchall()))

# ---- 4. 各表 symbol 可解析率（用 stock_names.code） ----
P("\n===== 4. symbol 可解析率 =====")
if "stock_names" in tabs:
    sncols = cols("stock_names")
    P(f"  stock_names 列: {sncols}")
    key = "code" if "code" in sncols else ("symbol" if "symbol" in sncols else None)
    if key:
        for t in ["positions", "trades", "news", "posts"]:
            tc = cols(t) if t in tabs else []
            if "symbol" not in tc: continue
            tot = cur.execute(f'SELECT COUNT(DISTINCT symbol) FROM "{t}" WHERE symbol IS NOT NULL AND symbol<>\'\'').fetchone()[0]
            res = cur.execute(f'SELECT COUNT(DISTINCT p.symbol) FROM "{t}" p JOIN stock_names s ON p.symbol=s."{key}" WHERE p.symbol IS NOT NULL AND p.symbol<>\'\'').fetchone()[0]
            P(f"  {t}: 不同代码 {tot} 个，可解析 {res} ({100*res/tot if tot else 0:.0f}%)")

# ---- 5. 重复与垃圾 ----
P("\n===== 5. 重复 / 垃圾 =====")
for t in ["positions", "trades", "news", "posts"]:
    if t not in tabs: continue
    tc = cols(t)
    keycands = [c for c in ["player", "author", "name", "symbol", "site", "post_url", "url", "action", "channel"] if c in tc]
    if not keycands: continue
    g = ", ".join(f'"{c}"' for c in keycands)
    tot = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    dist = cur.execute(f'SELECT COUNT(*) FROM (SELECT DISTINCT {g} FROM "{t}")').fetchone()[0]
    P(f"  {t}: 总 {tot} 行，按({','.join(keycands)})去重后 {dist} 行，重复 {tot-dist} 行")

# trades 里 symbol 为空的
if "trades" in tabs:
    n = cur.execute("SELECT COUNT(*) FROM trades WHERE symbol IS NULL OR symbol=''").fetchone()[0]
    P(f"  trades 空 symbol: {n}")
    if n:
        for r in cur.execute("SELECT author, symbol_name, action FROM trades WHERE symbol IS NULL OR symbol='' LIMIT 8").fetchall():
            P(f"      {dict(r)}")

con.close()

# ---- 6. 自动更新链路覆盖 ----
P("\n===== 6. 自动更新链路覆盖 =====")
P("  run_backfill_local.py ALL_SITES: eastmoney, sina, ths, taoguba, xueqiu")
for f in ["lhb_fetch.py", "jsl_fetch.py"]:
    p = os.path.join(BASE, f)
    if os.path.exists(p):
        age = (now.timestamp() - os.path.getmtime(p)) / 86400
        P(f"  {f}: 存在（脚本自身 {age:.1f} 天前修改）")
    else:
        P(f"  {f}: 不存在")

# ingest 注册了哪些 site
ingp = os.path.join(BASE, "viewer", "ingest.py")
if os.path.exists(ingp):
    src = open(ingp, encoding="utf-8").read()
    regs = sorted(set(re.findall(r'"(eastmoney|sina|ths|taoguba|xueqiu|jisilu|moer|lhb)"\s*:', src)))
    P(f"  ingest.py 注册的 site 键: {regs}")

# ---- 7. 日志/状态文件时效 ----
P("\n===== 7. 运行痕迹时效 =====")
for f in ["auto_update_status.json", "auto_update.log", "run_backfill_local.log"]:
    p = os.path.join(BASE, f)
    if os.path.exists(p):
        m = datetime.datetime.fromtimestamp(os.path.getmtime(p))
        P(f"  {f}: 最后修改 {m:%Y-%m-%d %H:%M}（{(now-m).total_seconds()/3600:.1f} 小时前）")
    else:
        P(f"  {f}: 不存在")

# ---- 8. 磁盘 ----
P("\n===== 8. 磁盘占用 =====")
V = os.path.join(BASE, "viewer")
for n in ["app.db", "app.db-wal", "app.db-shm"]:
    p = os.path.join(V, n)
    P(f"  {n}: {os.path.getsize(p)/1048576:.1f} MB" if os.path.exists(p) else f"  {n}: -")
baks = [f for f in os.listdir(V) if f.startswith("app.db.bak")]
tot = sum(os.path.getsize(os.path.join(V, f)) for f in baks)
P(f"  备份: {len(baks)} 个, {tot/1048576:.0f} MB")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("\nWROTE", OUT)
