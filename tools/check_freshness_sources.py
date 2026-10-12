# -*- coding: utf-8 -*-
"""第二支探针：区分「数据源查找失败 / 缺失 / 未启用 / 示例数据」四种可能。

关键判据：
  ① 代码里是否存在 eastmoney/ths/xueqiu 的抓取适配器（存在=支持但未启用；不存在=真缺失）
  ② 这些来源有没有任何真实抓取痕迹（raw_pages 只剩 taoguba → 没有）
  ③ players / posts / positions 里这些来源的行，URL 是不是 example.local（→ 示例数据）
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from viewer.db import Database  # noqa: E402

db = Database()

print("=" * 70)
print("[A] 抓取适配器：代码里到底支持哪些站点")
for pat in ("viewer/*.py", "viewer/**/*.py", "sites/*.py", "adapters/*.py"):
    for p in sorted(glob.glob(str(ROOT / pat), recursive=True)):
        print("   文件:", os.path.relpath(p, ROOT))
grep_terms = ["eastmoney", "xueqiu", "ths", "tonghuashun", "taoguba"]
print()
print("  各站点名在 viewer/ 代码里的出现次数（源码层面是否有适配逻辑）：")
src = {}
for p in glob.glob(str(ROOT / "viewer" / "**" / "*.py"), recursive=True):
    src[os.path.relpath(p, ROOT)] = open(p, encoding="utf-8").read()
for t in grep_terms:
    hits = {k: v.lower().count(t) for k, v in src.items() if v.lower().count(t)}
    print(f"    {t:<12}", hits or "无")

print()
print("  抓取器配置里的 sites（真正会被抓的）：")
try:
    from viewer import server  # noqa: E402
    cfg = server.load_config()
    sites = cfg.get("sites") or {}
    for k, v in sites.items():
        print(f"    {k:<12} enabled={v.get('enabled')} requires_vpn={v.get('requires_vpn')} "
              f"base={str(v.get('base_url') or v.get('url') or '')[:44]}")
    print("    配置键:", list(cfg.keys()))
except Exception as e:
    print("    读取失败:", e)

print()
print("=" * 70)
print("[B] 真实抓取痕迹：raw_pages 只有 taoguba，其余来源从未被真实抓取")
for r in db.conn.execute(
        "SELECT site, COUNT(*) n, MIN(fetched_at) mn, MAX(fetched_at) mx FROM raw_pages "
        "GROUP BY site").fetchall():
    print(f"    {r['site']:<12} 页数={r['n']:<5} 最早={r['mn']} 最近={r['mx']}")

print()
print("=" * 70)
print("[C] 示例数据识别：URL 里带 example.local / demo 的行数")
for tbl, col in (("players", "source_url"), ("posts", "post_url"),
                 ("trades", "post_url"), ("positions", "post_url")):
    cols = [x["name"] for x in db.conn.execute(f"PRAGMA table_info({tbl})")]
    if col not in cols:
        print(f"    {tbl:<10} 无 {col} 列（列={cols[:6]}…）")
        continue
    tot = db.conn.execute(f"SELECT COUNT(*) n FROM {tbl}").fetchone()["n"]
    demo = db.conn.execute(
        f"SELECT COUNT(*) n FROM {tbl} WHERE COALESCE({col},'') LIKE '%example%' "
        f"OR COALESCE({col},'') LIKE '%demo%'").fetchone()["n"]
    print(f"    {tbl:<10} 总 {tot:<5} 含 example/demo 的 {demo}")

print()
print("  逐来源看 posts 的 URL 样本（真实来源应有真实域名）：")
for r in db.conn.execute(
        "SELECT site, COUNT(*) n, MIN(post_url) sample FROM posts GROUP BY site").fetchall():
    print(f"    {r['site']:<12} n={r['n']:<4} 样本={str(r['sample'])[:64]}")

print()
print("  players 逐 platform 的 source_url 样本：")
for r in db.conn.execute(
        "SELECT platform, COUNT(*) n, MIN(source_url) sample FROM players "
        "GROUP BY platform").fetchall():
    print(f"    {r['platform']:<12} n={r['n']:<4} 样本={str(r['sample'])[:64]}")

print()
print("=" * 70)
print("[D] meta 里是否有「载入示例」的痕迹")
for k in ("last_ingest", "last_fetch_at", "demo_loaded_at", "last_demo_at", "demo_at"):
    v = db.get_meta(k)
    if v:
        print(f"    {k:<16} = {str(v)[:100]}")
allkeys = [r["key"] for r in db.conn.execute("SELECT key FROM meta ORDER BY key")]
print("    meta 全部键:", allkeys)

print()
print("=" * 70)
print("[E] refresh_status 里**不存在**的来源（= 前端判「未纳入刷新」的那些）")
have = {r["source_key"] for r in db.get_refresh_status()}
print("    refresh_status 现有 source_key:", sorted(have))
used = set()
for tbl, col in (("players", "platform"), ("positions", "site"),
                 ("posts", "site"), ("trades", "site")):
    for r in db.conn.execute(f"SELECT DISTINCT {col} v FROM {tbl} WHERE {col} IS NOT NULL"):
        used.add(r["v"])
missing = sorted(used - have)
print("    业务数据里出现过、但 refresh_status 无记录的来源:", missing)
for m in missing:
    print(f"      · {m:<12} 在 refresh_status 中{'不存在 → 前端显示「未纳入刷新」' if m not in have else ''}")
