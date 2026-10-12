# -*- coding: utf-8 -*-
"""P0-2 回填：用库里现成的 raw_text 把历史实盘交易的价格 / 数量重抽一遍。

背景（2026-10-05 测试报告 P0-2）：trades 表 268 行里 price / quantity 全为 NULL，
因为 taoguba 解析器的 _extract_trades() 从来不抽量价。现在解析器已补齐，
历史数据不用重新抓——原文 raw_text 就在库里，纯本地重抽即可。

用法：
    .venv\\Scripts\\python.exe tools\\backfill_trade_pq.py            # 预演，不改库
    .venv\\Scripts\\python.exe tools\\backfill_trade_pq.py --apply     # 真写库

只覆盖「原来为空」的字段：已经有的值一律不动，避免把老数据改坏。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from scraper.parsers.taoguba import TaogubaParser  # noqa: E402

DB = os.path.join(ROOT, "viewer", "app.db")
APPLY = "--apply" in sys.argv


def main() -> int:
    if not APPLY:
        print("【预演模式】只统计，不写库。加 --apply 才真写。\n")

    if APPLY:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = f"{DB}.bak-pq-{stamp}"
        shutil.copy2(DB, bak)
        print(f"库已备份 -> {os.path.basename(bak)} ({os.path.getsize(bak)/1024/1024:.1f} MB)")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    parser = TaogubaParser(cfg={})

    rows = con.execute(
        "SELECT id, author, action, symbol, symbol_name, price, quantity, buy_price, raw_text "
        "FROM trades ORDER BY id"
    ).fetchall()

    changed, filled_price, filled_qty, filled_buy_price = [], 0, 0, 0
    for r in rows:
        text = r["raw_text"] or ""
        hit = None
        for t in parser._extract_trades(text):
            sym = t.get("symbol") or t.get("symbol_name")
            same = (str(sym) == str(r["symbol"] or r["symbol_name"])
                    or str(t.get("symbol_name")) == str(r["symbol_name"]))
            if same and t.get("price") is not None or (same and t.get("quantity") is not None):
                hit = t
                break
        if not hit:
            continue
        hp, hq = hit.get("price"), hit.get("quantity")
        old_p, old_q = r["price"], r["quantity"]
        old_bp = r["buy_price"]
        new_p, new_q, new_bp = old_p, old_q, old_bp
        if hp is not None and old_p is None:
            new_p = hp
            filled_price += 1
        if hq is not None and old_q is None:
            new_q = hq
            filled_qty += 1
        if hp is not None and old_bp is None:
            new_bp = hp
            filled_buy_price += 1
        if (new_p, new_q, new_bp) != (old_p, old_q, old_bp):
            changed.append((r["id"], r["author"], r["symbol_name"], old_p, new_p,
                            old_q, new_q, old_bp, new_bp))

    print(f"扫描 {len(rows)} 行 | 抽到价格 {filled_price} | 抽到数量 {filled_qty} | "
          f"顺带补 buy_price {filled_buy_price} | 待更新 {len(changed)} 行")
    for c in changed:
        print(f"  row {c[0]:<6} {c[1][:14]:<14} {str(c[2])[:8]:<8} "
              f"price {c[3]} -> {c[4]} | qty {c[5]} -> {c[6]} | buy_price {c[7]} -> {c[8]}")

    if APPLY and changed:
        con.executemany(
            "UPDATE trades SET price=?, quantity=?, buy_price=? WHERE id=?",
            [(c[4], c[6], c[8], c[0]) for c in changed],
        )
        con.commit()
        print(f"\n已写库 {len(changed)} 行。")
    elif not APPLY:
        print("\n（预演：以上为将要发生的变更）")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
