# -*- coding: utf-8 -*-
"""龙虎榜**明细三表**（席位/营业部/机构）单日定点回补（2026-10-30，D33）。

【为什么不复用 tools/backfill_lhb.py】
那个脚本文件头明写：只回补 `lhb_stocks`（个股榜单，回测唯一需要的表），
席位/营业部/机构三表「体量大、逐日 3 次请求，留作最近窗口增量，不回补」。
而这次缺的恰恰是这三张表，且只缺**一天** —— 所以需要一个「只补指定交易日明细」的入口。

【为什么需要它（成因）】
`lhb_daily.py` 是唯一的日更编排入口，它把活交给 `lhb_fetch.py --days 8`（**从今天往前数
自然日**，没有单日参数）。于是有两种漏法：
  ① lhb_daily 当天没跑成，那么**那一天的四张表全空**；
  ② 跑过但当天个别接口失败（`lhb_fetch` 对每张明细表单独 try/except，失败就把该表写成
     0 行还 rc=0），于是**主榜有、明细空**——2026-10-29 就是这种。
②尤其阴险：进程退出码正常、日志只有一行「接口失败 xxx」，没人会发现。

⚠️ **2026-10-30 修正一处说法：① 的措辞之前写的是「11 天里它一直停在 skip: pre_publish/未完成」，
那是猜的，实测更严重** —— lhb_daily 从 2026-10-19 起**一次都没真正跑过**：`maybe_daily()` 起
daemon 线程后立即 return，而它的调用方 `tools/sched_job.py` 是**短命子进程**，进程退出时
CPython 把 daemon 线程直接杀掉（191 次 `{'started'}` 全是 0.1s，`last_run_at` 与
`lhb_daily.log` 双双冻结在 10-19）。已修（lhb_daily 改同步入口 `run_daily_sync`），
详见 DECISIONS.md D33 第六节。
顺带纠正：既然它自 10-19 起是空转的，那 **10-20~10-28 那些明细不可能是它写的**；
本项目里另一个跑 `lhb_fetch.py --days 6` 的是 `auto_update.py:114`，但它**没注册在 server 调度里**
（grep 0 命中），由外部/手工驱动——所以"10-29 到底谁跑的 lhb_fetch"这个问题，
得去 Windows 计划任务那边查，本仓库里没有答案。

【做法】
直接复用 `lhb_fetch` 里三个已验证的抓取函数（不复制请求逻辑、不造数据），按日：
  查→删当日→批量 INSERT OR IGNORE→提交，一天一 commit（短事务，避免与回补进程抢锁）。
`--dry-run` 只抓不写，用于先确认数据源当日到底有没有数据。

用法：
  python tools/backfill_lhb_detail.py --date 2026-10-29 --dry-run
  python tools/backfill_lhb_detail.py --date 2026-10-29
  python tools/backfill_lhb_detail.py --date 2026-10-28,2026-10-29
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import lhb_fetch as L                    # noqa: E402  抓取逻辑唯一真源

DB = os.environ.get("VIEWER_DB") or os.path.join(BASE, "viewer", "app.db")
DETAIL_TABLES = ("lhb_seats", "lhb_depts", "lhb_inst")
FETCHERS = {"lhb_seats": L.fetch_seats, "lhb_depts": L.fetch_depts, "lhb_inst": L.fetch_inst}
RETRIES = 5                              # database is locked 时的退避重试
BACKOFF_SEC = 2.0


def _retry(fn, what: str):
    """执行一次写库动作；遇 database is locked 退避重试（不动别人的连接）。"""
    last = None
    for attempt in range(RETRIES):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            last = e
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            print("  [retry %d/%d] %s：%s" % (attempt + 1, RETRIES, what, e), flush=True)
            time.sleep(BACKOFF_SEC)
    raise last  # type: ignore[misc]


def _count(con, table: str, day: str) -> int:
    return con.execute("SELECT COUNT(*) FROM %s WHERE trade_date=?" % table, (day,)).fetchone()[0]


def backfill_day(con: sqlite3.Connection, day: str, dry_run: bool = False) -> dict:
    """补一天的三张明细表。返回 {table: {before, after, fetched}}。"""
    out = {}
    for table in DETAIL_TABLES:
        before = _count(con, table, day)
        try:
            rows = FETCHERS[table](day)
            err = None
        except Exception as e:                    # noqa: BLE001
            rows, err = [], "%s: %s" % (type(e).__name__, str(e)[:160])
        info = {"before": before, "fetched": len(rows), "after": before, "error": err}
        if err:
            out[table] = info
            print("  %-10s 抓取失败 → 不动库（%s）" % (table, err), flush=True)
            continue
        if dry_run:
            out[table] = info
            print("  %-10s 现状 %3d 行 · 抓到 %3d 行（dry-run，未写库）"
                  % (table, before, len(rows)), flush=True)
            continue
        # 幂等：先删当日（重复跑得到同一结果），再批量插；一天一 commit
        _retry(lambda: con.execute("DELETE FROM %s WHERE trade_date=?" % table, (day,)), table + ".delete")
        _retry(lambda: L._insert(con, table, rows, ignore=True), table + ".insert")
        _retry(con.commit, table + ".commit")
        after = _count(con, table, day)
        info["after"] = after
        out[table] = info
        print("  %-10s 现状 %3d 行 · 抓到 %3d 行 · 写后 %3d 行"
              % (table, before, len(rows), after), flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="龙虎榜明细三表单日定点回补（真实数据、幂等）")
    ap.add_argument("--date", required=True, help="交易日，逗号分隔：2026-10-29 或 2026-10-28,2026-10-29")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--dry-run", action="store_true", help="只抓不写（先确认数据源当日是否有数据）")
    args = ap.parse_args()

    days = [d.strip() for d in args.date.split(",") if d.strip()]
    con = sqlite3.connect(args.db, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")     # 等锁 30s，配合 _retry 的 5 次退避
    try:
        L.ensure_schema(con)                     # 幂等补表/补列（正常是空操作）
        summary = {}
        for day in days:
            print("[lhb-detail] %s%s" % (day, "（dry-run）" if args.dry_run else ""), flush=True)
            summary[day] = backfill_day(con, day, dry_run=args.dry_run)
        bad = [t for d in summary.values() for t, v in d.items() if v.get("error")]
        print("\n[lhb-detail] 汇总：")
        for day, d in summary.items():
            print("  %s " % day + " · ".join("%s %d→%d" % (t, v["before"], v["after"])
                                             for t, v in d.items()), flush=True)
        if bad:
            print("[lhb-detail] 有接口失败：%s" % ",".join(sorted(set(bad))), flush=True)
            return 1
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
