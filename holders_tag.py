# -*- coding: utf-8 -*-
"""股东数据「风险隔离」打标（可重复执行、幂等）。

背景
----
十大股东名单里混着两类完全不同的自然人：
  ① 公司实控人 / 创始人家族（父子、夫妻、兄弟同持）—— 这不是二级市场投资行为，
     把它当成"牛散/游资"会严重误导判断；
  ② 真正的二级市场牛散 / 游资（跨多只股票、单股占比低）。

本脚本给每条股东记录打上 `holder_kind` + `exclude_reason`，让下游（牛散榜、
协同股、变动流、个股详情）都能用统一口径过滤，避免污染从不同出口漏出。

规则（按优先级）
----------------
  controlling  单股持股占比 >= 10%                → 实控人/举牌级，非二级市场投资者
  family       同股同期同姓氏自然人 >= 3 人
               且该姓氏合计占比 >= 20%            → 家族持股
  star         跨 >= 2 只股票出现的自然人          → 真牛散/游资级（有追踪价值）
  single       其余自然人                          → 单股散户，无追踪价值
  org          非自然人（机构/基金/社保/QFII 等）  → 不参与牛散口径

用法
----
  python holders_tag.py            # 打标（自动备份）
  python holders_tag.py --dry      # 只预览，不写库
"""
from __future__ import annotations

import argparse
import collections
import datetime
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "viewer", "app.db")

RATIO_CONTROLLING = 10.0   # 单股占比 >= 10% → 实控人级
FAMILY_MIN_PEOPLE = 3      # 同姓 >= 3 人
FAMILY_MIN_RATIO = 20.0    # 同姓合计占比 >= 20%
STAR_MIN_CROSS = 2         # 跨 >= 2 只股票 → 真牛散


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "--", "—", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def compute(rows):
    """返回 (kind_by_rid, reason_by_rid, family_detail, person_codes)"""
    person_codes = collections.defaultdict(set)
    for r in rows:
        if r["holder_type"] == "自然人" and r["holder_name"]:
            person_codes[r["holder_name"].strip()].add(r["symbol"])
    cross_n = {k: len(v) for k, v in person_codes.items()}

    # 按 (股票, 报告期) 聚合自然人 → 找同姓氏家族
    by_grp = collections.defaultdict(list)
    for r in rows:
        if r["holder_type"] == "自然人" and r["holder_name"]:
            by_grp[(r["symbol"], r["end_date"])].append(r)

    family_members = set()
    family_detail = []
    for (sym, ed), lst in by_grp.items():
        if len(lst) < FAMILY_MIN_PEOPLE:
            continue
        sur = collections.defaultdict(list)
        for r in lst:
            nm = (r["holder_name"] or "").strip()
            if len(nm) >= 2:
                sur[nm[0]].append(r)
        for s_, grp in sur.items():
            if len(grp) < FAMILY_MIN_PEOPLE:
                continue
            ratio_sum = sum((num(x["hold_ratio"]) or 0) for x in grp)
            if ratio_sum >= FAMILY_MIN_RATIO:
                for x in grp:
                    family_members.add(x["rid"])
                family_detail.append((sym, lst[0]["symbol_name"], ed, s_,
                                      len(grp), round(ratio_sum, 2)))

    kind_by_rid, reason_by_rid = {}, {}
    for r in rows:
        rid, nm = r["rid"], (r["holder_name"] or "").strip()
        ratio = num(r["hold_ratio"])
        if r["holder_type"] != "自然人":
            kind_by_rid[rid], reason_by_rid[rid] = "org", ""
        elif ratio is not None and ratio >= RATIO_CONTROLLING:
            kind_by_rid[rid] = "controlling"
            reason_by_rid[rid] = "单股持股≥10%（实控人/举牌级，非二级市场投资者）"
        elif rid in family_members:
            kind_by_rid[rid] = "family"
            reason_by_rid[rid] = "同股同期同姓氏≥3人且合计≥20%（家族持股）"
        elif cross_n.get(nm, 0) >= STAR_MIN_CROSS:
            kind_by_rid[rid], reason_by_rid[rid] = "star", ""
        else:
            kind_by_rid[rid], reason_by_rid[rid] = "single", ""
    return kind_by_rid, reason_by_rid, family_detail, person_codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只预览不写库")
    args = ap.parse_args()

    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cols = [r[1] for r in cur.execute("PRAGMA table_info(stock_holders)")]
    if not args.dry:
        if "holder_kind" not in cols:
            cur.execute("ALTER TABLE stock_holders ADD COLUMN holder_kind TEXT")
        if "exclude_reason" not in cols:
            cur.execute("ALTER TABLE stock_holders ADD COLUMN exclude_reason TEXT")
        con.commit()

    rows = [dict(r) for r in cur.execute(
        "SELECT rowid AS rid, symbol, symbol_name, holder_name, holder_type, "
        "end_date, scope, holder_rank, hold_ratio, free_ratio, hold_change "
        "FROM stock_holders")]

    kind_by_rid, reason_by_rid, family_detail, person_codes = compute(rows)

    stat = collections.Counter(kind_by_rid.values())
    star_people = collections.Counter()
    for r in rows:
        if kind_by_rid[r["rid"]] == "star":
            star_people[r["holder_name"].strip()] += 1

    print("===== 打标分布（共 %d 条）=====" % len(rows))
    for k in ("org", "single", "controlling", "family", "star"):
        print("  %-12s %6d" % (k, stat.get(k, 0)))
    print()
    print("===== 家族识别：%d 组（涉及 %d 条记录）====="
          % (len(family_detail), stat.get("family", 0)))
    for x in sorted(family_detail, key=lambda y: -y[5])[:8]:
        print("  %s %-8s %s 姓%s %s人 合计%.2f%%"
              % (x[0], (x[1] or "")[:8], x[2], x[3], x[4], x[5]))
    print()
    print("===== 隔离后「真牛散」：%d 人 =====" % len(star_people))
    for nm, n in star_people.most_common(12):
        print("  %-12s 出现%2s次  持有%s只  %s"
              % (nm[:12], n, len(person_codes[nm]),
                 " ".join(sorted(person_codes[nm])[:8])))

    if args.dry:
        print("\n[dry-run] 未写库")
        con.close()
        return 0

    cur.executemany(
        "UPDATE stock_holders SET holder_kind=?, exclude_reason=? WHERE rowid=?",
        [(kind_by_rid[r["rid"]], reason_by_rid[r["rid"]], r["rid"]) for r in rows])
    cur.execute("CREATE INDEX IF NOT EXISTS idx_holders_kind "
                "ON stock_holders(holder_kind, holder_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_holders_name_kind "
                "ON stock_holders(holder_name, holder_kind)")
    con.commit()

    chk = dict(cur.execute("SELECT holder_kind, COUNT(*) FROM stock_holders "
                           "GROUP BY holder_kind").fetchall())
    print("\n[已落库] 复核:", chk)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
