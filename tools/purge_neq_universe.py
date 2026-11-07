#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""清掉「新三板伪装北交所」的污染行（2026-11-05 P0 修复的配套动作）。

## 病根

`tools/fetch_a_shares_snapshot.py` 的 `DEFAULT_FS` 里北交所子句写成了
`m:0+t:81`（漏 `+s:2048`）。东财对这个 filter 返回的是**整个新三板**：

    GET clist?fs=m:0+t:81            -> total = 6765   （新三板 + 北交所）
    GET clist?fs=m:0+t:81+s:2048     -> total = 364    （只有北交所）

新三板与北交所代码段完全重叠（43/83/87/92 段两者通用），`units.stock_kind()`
只看代码段，判不出区别，于是 6464 只新三板被当成股票写进：

    stock_names        12030 行（其中 6464 行 market='北交所'）
    a_shares_universe  12030 行（其中 6464 行 board='北交所'）
    exclusion_pool      6407 行（no_kline 5668 —— 大头就是这批）

连锁后果：`screener._market_pool()` 由 5600 涨到 12030 → `kline_daily` 逐只拉新浪
（新三板在新浪无日线）→ 5860 只硬失败 → 每天判任务失败 → 健康度 degraded →
单轮白烧 2171s 且反复重试，真正的日更被挤在后面跑。

## 本脚本做什么

只做一件事：把「声称是北交所、但不在权威北交所名单里」的行删掉。

**权威名单怎么来**（三级，逐级降级，宁可不删）：
  1. 东财 `m:0+t:81+s:2048` 现拉（最权威，364 只）；
  2. 拉不到 → `security_master` 里 board='北交所' 的 code（本地主表，5589 行里的北交所）；
  3. 都拿不到 → **拒绝执行**（绝不能拿空名单去删，那会把真北交所一起删掉）。

## 用法

    python tools/purge_neq_universe.py              # 只算不写（默认 dry-run）
    python tools/purge_neq_universe.py --apply      # 真删，删前把待删行导出 JSON 备份
    python tools/purge_neq_universe.py --db path    # 指定库（默认 viewer/app.db）

删除前会把「待删行的完整内容」写成 `data/purge_neq_<时间戳>.json`，
回滚时按里面的行 INSERT 回去即可。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_DB = os.path.join(ROOT, "viewer", "app.db")
BACKUP_DIR = os.path.join(ROOT, "data")

# 表名 -> (板块列名, 主键列名)
TARGETS = {
    "stock_names": ("market", "code"),
    "a_shares_universe": ("board", "code"),
    # 2026-11-05 第二轮补漏：第一轮只清了「宇宙清单」两张表，漏了**行情快照**。
    # `stock_daily_snapshot` 11-03/11-04/11-05 三天各多出 6100+ 条新三板
    # （11-05：11988 行里 6425 行是北交所段，真实只有 5563 只 A 股）。
    # 这张表是 PE 分位、涨跌家数、市值分位、相对强弱的**唯一基准**，
    # 留着它就等于所有横截面统计都算在 12000 只的假分母上 —— 分位全错。
    # 11-02 及以前是干净的（北交所 348 行是真票），按 code 删不会伤到历史。
    "stock_daily_snapshot": ("board", "code"),
}
BSE_LABEL = "北交所"


def _authoritative_bse() -> tuple[set, str]:
    """权威北交所代码集。返回 (codes, 来源说明)；拿不到返回 (set(), 原因)。"""
    # 1) 东财 clist（必须带 +s:2048）
    try:
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from fetch_a_shares_snapshot import _fetch_all  # noqa: WPS433
        total, items = _fetch_all("m:0+t:81+s:2048", 1000)
        codes = {str(it.get("f12")) for it in items
                 if str(it.get("f12") or "").isdigit() and len(str(it.get("f12"))) == 6}
        if len(codes) >= 200:      # 北交所实际 360+ 只；低于 200 视为没取全，降级
            return codes, "eastmoney m:0+t:81+s:2048（total=%d）" % total
        print("[purge] 东财只回 %d 只，偏小 → 降级到本地主表" % len(codes), flush=True)
    except Exception as e:  # noqa: BLE001
        print("[purge] 东财取北交所名单失败：%s: %s" % (type(e).__name__, str(e)[:120]),
              flush=True)

    # 2) 本地 security_master
    try:
        from viewer import security_master as SM
        con = sqlite3.connect("file:%s?mode=ro" % DEFAULT_DB, uri=True)
        try:
            rows = con.execute(
                "SELECT code FROM security_master WHERE board=? AND kind='stock'",
                (BSE_LABEL,)).fetchall()
        finally:
            con.close()
        codes = {str(r[0]) for r in rows}
        if codes:
            return codes, "security_master.board='北交所'（%d 只）" % len(codes)
    except Exception as e:  # noqa: BLE001
        print("[purge] 读 security_master 失败：%s" % str(e)[:120], flush=True)

    return set(), "两条来源都拿不到"


def _rows_to_purge(con, table: str, label_col: str, keep: set) -> list:
    """该表里「标着北交所但不在 keep 里」的整行（returns list[dict]）。"""
    cols = [d[1] for d in con.execute("PRAGMA table_info(%s)" % table)]
    q = "SELECT %s FROM %s WHERE %s=?" % (",".join(cols), table, label_col)
    out = []
    for row in con.execute(q, (BSE_LABEL,)):
        d = dict(zip(cols, row))
        if str(d.get("code")) not in keep:
            out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="清掉新三板伪装北交所的污染行")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="真删（默认只算不写）")
    a = ap.parse_args()

    keep, src = _authoritative_bse()
    if not keep:
        print("[purge] 拒绝对空名单执行（否则会删掉真北交所）：%s" % src, file=sys.stderr)
        return 2
    print("[purge] 权威北交所名单 %d 只 ← %s" % (len(keep), src), flush=True)

    con = sqlite3.connect(a.db)
    con.execute("PRAGMA busy_timeout=15000")
    plan, total_rows = {}, 0
    for table, (label_col, _pk) in TARGETS.items():
        try:
            rows = _rows_to_purge(con, table, label_col, keep)
        except sqlite3.Error as e:
            print("[purge] 跳过 %s：%s" % (table, e), flush=True)
            continue
        plan[table] = rows
        total_rows += len(rows)
        print("[purge] %-20s 待删 %5d 行（保留北交所 %d 行）"
              % (table, len(rows), len(keep)), flush=True)

    if not total_rows:
        print("[purge] 没有需要清理的行 —— 库已是干净的", flush=True)
        con.close()
        return 0
    sample = [str(r.get("code")) for r in list(plan.values())[0][:5]]
    print("[purge] 样例：%s" % ", ".join(sample), flush=True)

    if not a.apply:
        print("[purge] dry-run：未写入。确认无误后加 --apply 执行。", flush=True)
        con.close()
        return 0

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(BACKUP_DIR, "purge_neq_%s.json" % stamp)
    with io.open(bak, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False)
    print("[purge] 待删行已备份 → %s" % bak, flush=True)

    try:
        con.execute("BEGIN")
        for table, (label_col, _pk) in TARGETS.items():
            codes = [str(r["code"]) for r in plan.get(table, [])]
            for i in range(0, len(codes), 800):
                chunk = codes[i:i + 800]
                con.execute("DELETE FROM %s WHERE code IN (%s) AND %s=?"
                            % (table, ",".join("?" * len(chunk)), label_col),
                            chunk + [BSE_LABEL])
        con.execute("COMMIT")
    except sqlite3.Error as e:
        con.execute("ROLLBACK")
        print("[purge] 删除失败已回滚：%s" % e, file=sys.stderr)
        con.close()
        return 1

    for table in TARGETS:
        try:
            n = con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            b = con.execute("SELECT COUNT(*) FROM %s WHERE %s=?" % (table, TARGETS[table][0]),
                            (BSE_LABEL,)).fetchone()[0]
            print("[purge] %-20s 现存 %5d 行（北交所 %d）" % (table, n, b), flush=True)
        except sqlite3.Error:
            pass
    con.close()
    print("[purge] 完成。回滚用：%s" % bak, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
