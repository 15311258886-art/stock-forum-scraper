# -*- coding: utf-8 -*-
"""宇宙一致性审计（只读，2026-10-31 决策④）。

## 为什么需要它

「可转债混进 K 线库」这件事被报告提过两次，但真正查下来，**K 线库是干净的**：
111024 在 kline 里只有 2005-2014 的 528 行（那是老代码体系里的另一只债券，
不是 2021 年才上市的澳弘转债），113710 根本不在 kline 里。

脏的是另外两张表，而它们都没人查：

  1. `stock_indicators_daily` —— 111024 有 273 行（2010-01 ~ 2014-09），
     名称却写着「澳弘转债」。同一个代码，一只 2014 年就退市的债券，
     被贴上了一只 2021 年才上市的可转债的名字。**名称与代码错配**。
  2. `a_shares_universe`（号称权威全量宇宙）——
     · 混进 2 只可转债（111024、113710），board 记为「其它」；
     · **漏收全部 328 只北交所 920 段**（它是 2023 年起启用的新代码段）。

第 2 条的后果比可转债严重得多：`selection.py` 只读这张表，虽然它有意排除北交所、
结果是"恰好"没受影响；但任何新写的取码逻辑只要信了「这张表是全量」，
就会静默丢掉整个北交所。

## 这个工具做什么

只读、只报告，**不删任何行、不改任何表**。产出四节：
  ① 指标库里的非股票标的
  ② 白名单里的非股票标的
  ③ 白名单漏收的、指标库里实际有数据的代码（按板块段汇总）
  ④ 同一代码在「指标库名称 / 名称表名称」之间不一致的

## 用法

    python tools/audit_universe.py           # 人读
    python tools/audit_universe.py --json    # 机读（供 CI 引用）
    python tools/audit_universe.py --strict  # 有 WARN 即 exit 1

退出码：0 = PASS 或仅 WARN；1 = --strict 且有 WARN；2 = 读库失败。
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

from viewer import units as U  # noqa: E402

DB = os.path.join(ROOT, "viewer", "app.db")


def _seg(code: str) -> str:
    """代码段前三/前二，用于把漏收清单压成可读的几行。"""
    c = str(code or "")
    for p in ("920", "688", "689", "600", "601", "603", "605",
              "000", "001", "002", "003", "300", "301", "302", "43", "83", "87", "88"):
        if c.startswith(p):
            return p
    return c[:2]


def collect(con: sqlite3.Connection) -> dict:
    # ① 指标库里的非股票
    ind_codes = {}
    for code, name in con.execute(
            "SELECT code, MAX(name) FROM stock_indicators_daily GROUP BY code"):
        ind_codes[code] = name or ""
    ind_nonstock = [
        {"code": c, "name": n, "kind": U.stock_kind(c, n)}
        for c, n in sorted(ind_codes.items())
        if U.stock_kind(c, n) != U.KIND_STOCK
    ]

    # ② 白名单里的非股票
    uni_rows = list(con.execute(
        "SELECT code, name, board, exchange FROM a_shares_universe"))
    uni_nonstock = [
        {"code": c, "name": n, "board": b, "exchange": e, "kind": U.stock_kind(c, n or "")}
        for c, n, b, e in uni_rows
        if U.stock_kind(c, n or "") != U.KIND_STOCK
    ]
    uni_codes = {c for c, _n, _b, _e in uni_rows}

    # ③ 白名单漏收（只统计指标库里真有数据的，白名单本来就含未上市/无K线的）
    missing = {}
    for c in ind_codes:
        if c in uni_codes:
            continue
        if U.stock_kind(c, ind_codes[c]) != U.KIND_STOCK:
            continue  # 非股票不进来，①已单独列
        missing.setdefault(_seg(c), []).append(c)
    missing_summary = [
        {"seg": s, "n": len(v), "sample": sorted(v)[:3]}
        for s, v in sorted(missing.items(), key=lambda kv: -len(kv[1]))
    ]

    # ④ 名称错配：同一个代码，指标库名 vs 名称表名
    name_map = {c: (n or "") for c, n in con.execute(
        "SELECT code, name FROM stock_names")}
    mismatch = []
    for c, n in ind_codes.items():
        other = name_map.get(c)
        if other is None:
            continue
        a, b = (n or "").strip(), other.strip()
        if a and b and a != b:
            mismatch.append({"code": c, "indicators_name": a, "names_table": b})
    mismatch.sort(key=lambda x: x["code"])

    return {
        "n_indicators": len(ind_codes),
        "n_universe": len(uni_codes),
        "indicators_nonstock": ind_nonstock,
        "universe_nonstock": uni_nonstock,
        "universe_missing": missing_summary,
        "universe_missing_total": sum(x["n"] for x in missing_summary),
        "name_mismatch": mismatch[:50],
        "name_mismatch_total": len(mismatch),
    }


def verdict_of(r: dict) -> tuple:
    """返回 (level, 说明)。WARN 的最低条件是「有非股票混进指标库」——
    因为它会静默影响任何长窗口统计。"""
    warns = []
    if r["indicators_nonstock"]:
        warns.append("指标库混入非股票 %d 只（长窗口回测会被静默计入）"
                     % len(r["indicators_nonstock"]))
    if r["universe_nonstock"]:
        warns.append("白名单混入非股票 %d 只" % len(r["universe_nonstock"]))
    if r["universe_missing_total"]:
        warns.append("白名单漏收指标库中已有的股票 %d 只" % r["universe_missing_total"])
    if r["name_mismatch_total"]:
        warns.append("名称错配 %d 处（同一代码两个名字）" % r["name_mismatch_total"])
    return ("WARN" if warns else "PASS"), warns


def main() -> int:
    ap = argparse.ArgumentParser(description="宇宙一致性审计（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true", help="有 WARN 即非零退出")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print("[audit_universe] 找不到 %s" % DB, file=sys.stderr)
        return 2
    try:
        con = sqlite3.connect(DB, timeout=30)
        r = collect(con)
        con.close()
    except sqlite3.Error as e:
        print("[audit_universe] 读库失败：%s" % e, file=sys.stderr)
        return 2

    level, warns = verdict_of(r)
    r["level"] = level
    r["warnings"] = warns

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 1 if (args.strict and level == "WARN") else 0

    print("=" * 72)
    print("宇宙一致性审计（只读，不改任何数据）")
    print("  指标库代码 %d 只 / 白名单 %d 只" % (r["n_indicators"], r["n_universe"]))
    print("=" * 72)

    print("\n① 指标库里的非股票标的：%d 只" % len(r["indicators_nonstock"]))
    for x in r["indicators_nonstock"][:20]:
        print("   %-8s %-14s %s" % (x["code"], x["name"], x["kind"]))
    if not r["indicators_nonstock"]:
        print("   （无）")

    print("\n② 白名单里的非股票标的：%d 只" % len(r["universe_nonstock"]))
    for x in r["universe_nonstock"][:20]:
        print("   %-8s %-14s board=%-8s %s" % (x["code"], x["name"], x["board"], x["kind"]))
    if not r["universe_nonstock"]:
        print("   （无）")

    print("\n③ 白名单漏收（指标库有数据、白名单没有）：%d 只"
          % r["universe_missing_total"])
    for x in r["universe_missing"]:
        print("   段 %-4s %4d 只  例: %s" % (x["seg"], x["n"], ", ".join(x["sample"])))
    if not r["universe_missing"]:
        print("   （无）")

    print("\n④ 名称错配（指标库名 ≠ 名称表名）：%d 处" % r["name_mismatch_total"])
    for x in r["name_mismatch"][:15]:
        print("   %-8s 指标库=%-14s 名称表=%s"
              % (x["code"], x["indicators_name"], x["names_table"]))
    if not r["name_mismatch"]:
        print("   （无）")

    print("\n" + "-" * 72)
    print("结论：%s" % level)
    for w in warns:
        print("  · WARN %s" % w)
    if level == "WARN":
        print("  注：本工具只报告，不删数据。是否修正（补白名单 / 剔债券 / 改名称）")
        print("      需人工决定 —— 自动清理会连带删掉真实历史行。")
    return 1 if (args.strict and level == "WARN") else 0


if __name__ == "__main__":
    sys.exit(main())
