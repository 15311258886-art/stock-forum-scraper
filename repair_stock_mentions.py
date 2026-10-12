# -*- coding: utf-8 -*-
"""重建「讨论股票」关联表 stock_mentions（离线，可重复执行）。

为什么需要这个脚本
------------------
`stock_mentions` 是 posts 的**派生表**（一条帖子里提到哪些 6 位代码），由抓取时写入。
它出过两个问题，都会让「当前讨论股票」「舆论情绪」两个板块近乎空转：

1. **抽取失效**（已修 viewer/analytics.py 的 CODE_RE）：
   旧正则用 `\\b` 做词边界，而 Python 的 `\\b` 把汉字当"词字符"，
   于是「宁德时代300750」这类中文里最常见的写法根本匹配不上。
   实测 1059 条帖子只抽出 3 个代码，改对后是 30 个。
2. **外键孤儿**：帖子曾被质量过滤/重建过，posts.id 变了，
   而 stock_mentions.post_id_ref 仍指向旧 id —— 实测 46 行里 33 行（72%）
   JOIN 不到任何帖子，等于计入的"讨论量"有一大半是幽灵数据。

本脚本按「派生数据可重算」的原则做**全量重建**（先删后插），
并在动手前对整库做一次事务一致快照，随时可回滚。

用法：
    .venv\\Scripts\\python.exe repair_stock_mentions.py            # 重建
    .venv\\Scripts\\python.exe repair_stock_mentions.py --dry-run  # 只统计不写库
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer.analytics import extract_codes   # noqa: E402
from viewer.db import Database               # noqa: E402
from viewer.stock_names import get_resolver  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="重建 stock_mentions（讨论股票关联表）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    db = Database()

    rows = db.conn.execute(
        "SELECT id, text FROM posts WHERE text IS NOT NULL AND text<>''"
    ).fetchall()

    before_total = db.conn.execute("SELECT COUNT(*) FROM stock_mentions").fetchone()[0]
    before_syms = db.conn.execute(
        "SELECT COUNT(DISTINCT symbol) FROM stock_mentions").fetchone()[0]
    orphan = db.conn.execute(
        "SELECT COUNT(*) FROM stock_mentions sm LEFT JOIN posts p ON p.id=sm.post_id_ref "
        "WHERE p.id IS NULL").fetchone()[0]

    print(f"[现状] 提及行 {before_total} · 覆盖代码 {before_syms} · 孤儿外键 {orphan}")

    # 名称解析：走名称解析器（config → 缓存表 → 库内反查 → 内置表），不联网。
    # 早先这里只查 stock_names 缓存表，于是内置表里明明有的代码（如 000568 泸州老窖）
    # 也被记成"名称待补全"。
    res = get_resolver(db=db, online=False)
    name_cache: dict[str, str] = {}

    def name_of(code: str) -> str:
        if code not in name_cache:
            try:
                name_cache[code] = res.resolve(code, allow_online=False).get("name", "") or ""
            except Exception:                                     # noqa: BLE001
                name_cache[code] = ""
        return name_cache[code]

    pairs: list[tuple[int, str, str, str]] = []
    for r in rows:
        text = r["text"] or ""
        for code in sorted(set(extract_codes(text))):             # 同一帖内去重
            pairs.append((r["id"], code, name_of(code), text[:120]))

    new_syms = len({p[1] for p in pairs})
    print(f"[重建] 可写入 {len(pairs)} 行 · 覆盖代码 {new_syms} 个（来自 {len(rows)} 条帖子）")

    if args.dry_run:
        print("[dry-run] 未写库。")
        return 0

    snap = db.snapshot(prefix="app.db.pre-mentionrepair")
    print(f"[快照] {snap}")

    db.conn.execute("DELETE FROM stock_mentions")
    db.conn.executemany(
        "INSERT INTO stock_mentions (post_id_ref, symbol, symbol_name, context) VALUES (?,?,?,?)",
        pairs,
    )
    db.conn.commit()

    # 收尾：把仍缺名的代码做一次离线名称补全（落 stock_names + 回填各表的 *_name 列），
    # 与抓取入库后的流程保持一致，避免"表重建好了、名称列还是空的"。
    try:
        bf = res.backfill_missing(online=False)
        print(f"[补名] 解析 {bf['requested']} 个代码，写入缓存 {bf['persisted']} 条")
    except Exception as e:                                        # noqa: BLE001
        print(f"[补名] 跳过（{e}）")

    after_total = db.conn.execute("SELECT COUNT(*) FROM stock_mentions").fetchone()[0]
    after_syms = db.conn.execute(
        "SELECT COUNT(DISTINCT symbol) FROM stock_mentions").fetchone()[0]
    after_orphan = db.conn.execute(
        "SELECT COUNT(*) FROM stock_mentions sm LEFT JOIN posts p ON p.id=sm.post_id_ref "
        "WHERE p.id IS NULL").fetchone()[0]
    missing_name = db.conn.execute(
        "SELECT COUNT(*) FROM stock_mentions WHERE symbol_name IS NULL OR symbol_name=''"
    ).fetchone()[0]

    print(f"[结果] 提及行 {after_total} · 覆盖代码 {after_syms} · 孤儿外键 {after_orphan} "
          f"· 仍无名称 {missing_name}")
    top = db.conn.execute(
        "SELECT symbol, MAX(symbol_name) nm, COUNT(*) n FROM stock_mentions "
        "GROUP BY symbol ORDER BY n DESC LIMIT 12").fetchall()
    for t in top:
        print(f"   {t['symbol']}  {t['nm'] or '（名称待补全）'}  {t['n']} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
