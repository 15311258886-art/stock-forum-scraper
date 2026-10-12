#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回填 posts.raw_page_id 断链（帖子 → 原始存档）。

背景（2026-09-26 审计发现）：
    db.save_raw_page() 用 `INSERT ... ON CONFLICT(site,url) DO UPDATE` 做同 URL 去重，
    但 `return cur.lastrowid` —— SQLite 的 last_insert_rowid() 只被 INSERT 改写，
    走 UPDATE 分支时它还是**更早那次** INSERT 的 rowid（或 0）。
    列表页 URL 会被反复抓取，所以 UPDATE 分支是常态 →
    1059 条帖子的 raw_page_id 全部指向不存在 / 别的页面的存档。

本脚本做什么：
    只用「内容校验」回填，绝不用 URL 前缀兜底（根路径 `https://guba.sina.com.cn/`
    能前缀匹配全部新浪帖，但页里根本没有这些帖 —— 那样只会把断链换成**错链**）。
    命中条件（任一成立才回填）：
      1) post_url 与存档 url 完全相同；
      2) post_id（≥6 位）出现在该站存档 HTML 里；
      3) 帖子正文前 12 字出现在该站存档纯文本里。
    其余保持不动（该页确实没存过档 → 无链可挂，由界面诚实显示"无存档"）。

用法：
    python repair_raw_page_links.py --dry-run   # 只统计，不写库
    python repair_raw_page_links.py             # 备份后写库
"""
from __future__ import annotations

import argparse
import html as _html
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "viewer" / "app.db"

_TAG = re.compile(r"<[^>]+>")
_DROP = re.compile(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", re.I)


def plain(h: str) -> str:
    return _html.unescape(_TAG.sub(" ", _DROP.sub("", h or "")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[ERR] 找不到数据库：{DB_PATH}")
        return 1

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    pages: dict[int, tuple[str, str, str, str]] = {}
    for r in cur.execute("SELECT id,site,url,html FROM raw_pages").fetchall():
        pages[r["id"]] = (r["site"], r["url"] or "", plain(r["html"]), r["html"] or "")
    print(f"存档页 {len(pages)} 条")

    stats = {"exact_url": 0, "post_id": 0, "text": 0, "unlinkable": 0}
    plan: list[tuple[int, int]] = []

    for p in cur.execute("SELECT id,site,post_id,post_url,text,raw_page_id FROM posts").fetchall():
        site = p["site"] or ""
        pid = str(p["post_id"] or "")
        pu = p["post_url"] or ""
        tx = (p["text"] or "").strip()
        hit_id, why = None, ""
        for rgid, (gs, gu, gt, gh) in pages.items():
            if gs != site:
                continue
            if pu and gu and pu == gu:
                hit_id, why = rgid, "exact_url"
                break
            if len(pid) >= 6 and pid in gh:
                hit_id, why = rgid, "post_id"
                break
            if len(tx) >= 12 and tx[:12] in gt:
                hit_id, why = rgid, "text"
                break
        if hit_id is None:
            stats["unlinkable"] += 1
            continue
        stats[why] += 1
        if p["raw_page_id"] != hit_id:
            # ⚠️ 顺序必须与下面 SQL 的占位符一致：SET raw_page_id=?（先）WHERE id=?（后）
            plan.append((hit_id, p["id"]))

    print("校验结果：")
    print(f"  post_url 完全相同 : {stats['exact_url']}")
    print(f"  post_id 命中存档   : {stats['post_id']}")
    print(f"  正文前 12 字命中   : {stats['text']}")
    print(f"  无存档可挂（保持）  : {stats['unlinkable']}")
    print(f"  → 待改写 raw_page_id 的行数：{len(plan)}")

    if args.dry_run:
        print("\n[DRY-RUN] 未写库。")
        return 0

    # WAL 模式下只 copy app.db 会丢 -wal 里未 checkpoint 的增量（可能拿到不一致快照）。
    # 正确做法：先 TRUNCATE checkpoint 把 WAL 归并进主库，再拷贝（此时仍持有连接锁）。
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.commit()
    bak = DB_PATH.with_name(f"app.db.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(DB_PATH, bak)
    print(f"\n已备份（WAL 已归并）：{bak}")

    if plan:
        cur.executemany("UPDATE posts SET raw_page_id=? WHERE id=?", plan)
        con.commit()
    print(f"已回填 {len(plan)} 行")

    ok = cur.execute(
        "SELECT COUNT(*) FROM posts p JOIN raw_pages r ON r.id=p.raw_page_id"
    ).fetchone()[0]
    tot = cur.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    print(f"最终：{tot} 帖中 {ok} 帖可追溯原始存档（{ok * 100 // max(tot, 1)}%）")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
