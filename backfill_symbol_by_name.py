# -*- coding: utf-8 -*-
"""一次性回填：trades/positions 里"有名无码"的记录，用名字反查代码。

背景（2026-09-29，王总反馈"吉鑫科技查不到"）：
  淘股吧源帖只写名字不写代码，解析器 symbol 恒为空 → 详情/K线/行情全链路断。
  而本地库（stock_names 缓存表 + 龙虎榜证券简称）明明有 代码↔名字 映射，只是从未反查。

用法：
    .venv/Scripts/python.exe backfill_symbol_by_name.py            # 本地 + 东财搜索兜底
    .venv/Scripts/python.exe backfill_symbol_by_name.py --offline  # 仅本地

口径：与 viewer.stock_names.name_to_code 完全一致（精确 → 黏连后缀 → 唯一前缀 → 联网）。
只更新 symbol 为空且 symbol_name 非空的行；未命中的保持原样（不编造）。
可重复执行（幂等：命中过的行 symbol 非空，自动跳过）。
"""
from __future__ import annotations

import argparse
import io
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from viewer.stock_names import name_to_code, sync_all_stock_names  # noqa: E402

DB_PATH = ROOT / "viewer" / "app.db"


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="不联网，仅本地反查")
    args = ap.parse_args()

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    class _DBWrap:  # name_to_code 只用到 .conn
        conn = db

    wrapper = _DBWrap()

    if not args.offline:
        # 先把全量 A 股 代码↔名称 灌进缓存表（suggest 接口已失效，改整表拉取；
        # 灌满后本地精确匹配即可覆盖全部真实股名，未命中的就真是解析噪音了）。
        try:
            n = sync_all_stock_names(wrapper)
            total = db.execute("SELECT COUNT(*) FROM stock_names").fetchone()[0]
            print(f"== 全量名称同步：新增 {n} 行，缓存表现共 {total} 行 ==")
        except Exception as e:  # noqa: BLE001 同步失败不阻断回填
            print(f"== 全量名称同步失败（继续用本地库回填）：{e} ==")

    fixed = skipped = 0
    unmatched: list[tuple[str, str, str]] = []
    for table, author_col in (("trades", "author"), ("positions", "player")):
        rows = db.execute(
            f"SELECT id, {author_col} AS who, symbol_name FROM {table} "
            f"WHERE (symbol IS NULL OR symbol='') AND symbol_name IS NOT NULL AND symbol_name<>''"
        ).fetchall()
        print(f"== {table}: 待回填 {len(rows)} 行 ==")
        for r in rows:
            nm = (r["symbol_name"] or "").strip()
            hit = name_to_code(nm, db=wrapper, allow_online=not args.offline)
            if hit:
                db.execute(f"UPDATE {table} SET symbol=? WHERE id=?", (hit["symbol"], r["id"]))
                print(f"  ✓ [{r['who']}] {nm} -> {hit['symbol']}（{hit['name']}，{hit['match']}）")
                fixed += 1
            else:
                unmatched.append((table, str(r["who"]), nm))
                skipped += 1
    db.commit()

    print(f"\n== 汇总 ==  回填 {fixed} 行；未命中 {skipped} 行（保持有名无码，不编造）")
    if unmatched:
        print("未命中样例（多为解析噪音片段，如句子截断）：")
        for t, who, nm in unmatched[:15]:
            print(f"  [{t}] {who}: {nm}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
