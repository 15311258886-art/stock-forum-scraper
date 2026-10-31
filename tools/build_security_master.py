# -*- coding: utf-8 -*-
"""建 / 重建 `security_master`（全市场证券主表）—— P0「全 A 股宇宙不完整」。

## 为什么需要这个脚本

`a_shares_universe` 号称"全量 A 股宇宙"，实际是 **5,240 行**：漏收全部 328 只北交所
920 段，还混着 2 只可转债和 1 只 ETF。任何信了"这张表是全量"的取码逻辑，
都会静默丢掉整个北交所 —— 不报错，只是永远选不出来。

本脚本把白名单、名称表、行情快照、指标库、K 线库、工商信息表全部扫一遍，
合成一张 `security_master`，并**如实标注**非股票标的（不删除）。

## 幂等

每次都写同一批行、只更新内容，并把"本次没扫到"的行删掉（派生表必须收敛）。
可以 cron 每天跑，反复跑行数不变。

## 用法

    python tools/build_security_master.py                 # 建/重建
    python tools/build_security_master.py --dry-run        # 只算不写
    python tools/build_security_master.py --json           # 机读统计
    python tools/build_security_master.py --sync-universe  # 同时把缺的股票补进白名单
                                                          # （只 INSERT，不删不改）

退出码：0 = 成功；2 = 读/写库失败；3 = 来源表全空（等于库里什么都没有，先查抓取）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import security_master as SM  # noqa: E402

DB = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")


def _print_report(stats: dict, db_path: str) -> None:
    print("=" * 72)
    print("security_master 重建完成 —— %s" % db_path)
    print("  as_of=%s  耗时 %ss  使用 K 线库：%s"
          % (stats["as_of"], stats["elapsed_sec"], stats["kline_used"] or "（无）"))
    print("=" * 72)
    print("总行数 %d（新增 %d / 更新 %d / 清理 %d）"
          % (stats["total"], stats["inserted"], stats["updated"], stats["orphans_removed"]))

    print("\n按 kind 分布：")
    for k, v in sorted(stats["by_kind"].items(), key=lambda kv: -kv[1]):
        print("   %-18s %5d" % (k, v))

    print("\n按 board 分布：")
    for b, v in sorted(stats["by_board"].items(), key=lambda kv: -kv[1]):
        print("   %-10s %5d" % (b, v))

    print("\n按 exchange 分布：")
    for e, v in sorted(stats["by_exchange"].items(), key=lambda kv: -kv[1]):
        print("   %-8s %5d" % (e, v))

    print("北交所股票：%d 只（P0 要求 ≥ 300，修复前白名单里只有 2 只）"
          % stats["stock_bse"])
    print("名称带退市标记：%d 只（is_active 仍为 1，按约定；用 delist_suspects() 过滤）"
          % stats.get("delist_suspects", 0))

    print("\n非股票标的（保留在表内、kind != stock，供消费方过滤）：%d 只"
          % len(stats["nonstock"]))
    for x in stats["nonstock"]:
        print("   %-8s %-22s %s" % (x["code"], x["name"], x["kind"]))
    if not stats["nonstock"]:
        print("   （无）")

    if "universe_inserted" in stats:
        print("\n已同步 a_shares_universe：补写 %d 行（只 INSERT，未删改任何旧行）"
              % stats["universe_inserted"])
    print("\n提示：消费方请改用 viewer.security_master.stock_codes(conn)，不要再读")
    print("      a_shares_universe（遗留表，本轮不动）。")


def main() -> int:
    ap = argparse.ArgumentParser(description="建/重建 security_master（全市场证券主表）")
    ap.add_argument("--db", default=DB, help="SQLite 路径（默认 viewer/app.db）")
    ap.add_argument("--kline-db", default=None,
                    help="K 线库路径（默认探测 data/kline_daily.db）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--sync-universe", action="store_true",
                    help="把主表里、白名单缺的股票补进 a_shares_universe（只 INSERT）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 统计")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("[build_security_master] 找不到 %s" % args.db, file=sys.stderr)
        return 2

    con = None
    try:
        con = sqlite3.connect(args.db, timeout=60)
        con.execute("PRAGMA busy_timeout=60000")
        stats = SM.build(con, kline_db=args.kline_db,
                         dry_run=args.dry_run, sync_universe=args.sync_universe)
    except SM.SecurityMasterMissing as e:
        print("[build_security_master] %s" % e, file=sys.stderr)
        return 3
    except sqlite3.Error as e:
        print("[build_security_master] 读/写库失败：%s" % e, file=sys.stderr)
        return 2
    finally:
        if con is not None:
            con.close()

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
    else:
        _print_report(stats, args.db)
        if args.dry_run:
            print("\n（--dry-run：以上为试算结果，未写入数据库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
