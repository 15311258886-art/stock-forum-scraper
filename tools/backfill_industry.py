# -*- coding: utf-8 -*-
"""行业映射批量回补（2026-10-30）。

背景：`stock_org` 只有 398 只（因子宇宙 5,338 只的 7.5%），而 predictor 的行业反查
（`_industry_index`）只读这张表。它同时喂政策/板块资金/海外映射与热板块 gate，
所以“股票池全市场”并不等于“行业约束也全市场”。

做法：东财 RPT_F10_BASIC_ORGINFO 支持 `SECUCODE in (...)` 批量过滤（已实测），
50 只/批取 EM2016（东财行业三段式）、INDUSTRYCSRC1（证监会行业）与 LISTING_DATE，
只补空字段，不覆盖已有行业。

用法：
  python tools/backfill_industry.py --dry-run --limit 100
  python tools/backfill_industry.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "viewer", "app.db")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "viewer") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "viewer"))

import emapi  # noqa: E402

BATCH = 50
# 与 ann_feed 同口径：六位数字里仍可能混入 111xxx 转债，只认正股代码形态。
_A_SHARE = re.compile(r"^(60|68|00|30|43|83|87|88|92)\d{4}$")


def _secucode(code: str) -> str:
    c = "".join(ch for ch in str(code or "") if ch.isdigit()).zfill(6)
    if c.startswith(("43", "83", "87", "88", "92")):
        return c + ".BJ"
    if c.startswith(("6", "5", "9")):
        return c + ".SH"
    return c + ".SZ"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(DB, timeout=120)
    con.row_factory = sqlite3.Row
    codes = [r["code"] for r in con.execute(
        "SELECT DISTINCT code FROM stock_indicators_daily "
        "WHERE code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'")
        if _A_SHARE.match(str(r["code"] or ""))]
    have = {r["code"]: (r["industry"] or "", r["industry_csrc"] or "")
            for r in con.execute("SELECT code, industry, industry_csrc FROM stock_org")}
    need_all = [c for c in codes if not all(have.get(c, ("", "")))]
    need = need_all[:args.limit] if args.limit else need_all
    have_ind = sum(1 for c in codes if have.get(c, ("", ""))[0])
    have_csrc = sum(1 for c in codes if have.get(c, ("", ""))[1])
    print("宇宙 %d 只｜已有行业 %d 只｜已有证监会行业 %d 只｜待补 %d 只｜本次处理 %d 只"
          % (len(codes), have_ind, have_csrc, len(need_all), len(need)), flush=True)
    if args.dry_run:
        print("dry-run：只打印，不写库")
        return 0

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    filled = 0
    for i in range(0, len(need), BATCH):
        chunk = need[i:i + BATCH]
        flt = "(SECUCODE in (%s))" % ",".join('"%s"' % _secucode(c) for c in chunk)
        try:
            r = emapi.datacenter("RPT_F10_BASIC_ORGINFO", flt=flt, page_size=BATCH)
        except Exception as e:  # noqa: BLE001
            print("批次 %d 异常（跳过）：%s" % (i // BATCH + 1, e), flush=True)
            continue
        if not r.get("ok"):
            print("批次 %d 失败（跳过）：%s" % (i // BATCH + 1, r.get("err")), flush=True)
            continue
        by_sc = {_secucode(c): c for c in chunk}
        rows = []
        for x in r.get("rows") or []:
            code = by_sc.get(str(x.get("SECUCODE") or ""))
            if not code:
                continue
            ind = str(x.get("EM2016") or "").strip()
            csrc = str(x.get("INDUSTRYCSRC1") or "").strip()
            ld = str(x.get("LISTING_DATE") or "")[:10]
            if not ind and not csrc and not ld:
                continue
            rows.append((code, ind, csrc, ld, now))
        if rows:
            con.executemany(
                "INSERT INTO stock_org(code,industry,industry_csrc,list_date,source,updated_at) "
                "VALUES(?,?,?,?,'eastmoney_f10_batch',?) "
                "ON CONFLICT(code) DO UPDATE SET "
                " industry=CASE WHEN IFNULL(stock_org.industry,'')='' "
                "           THEN excluded.industry ELSE stock_org.industry END,"
                " industry_csrc=CASE WHEN IFNULL(stock_org.industry_csrc,'')='' "
                "           THEN excluded.industry_csrc ELSE stock_org.industry_csrc END,"
                " list_date=CASE WHEN IFNULL(stock_org.list_date,'')='' "
                "           THEN excluded.list_date ELSE stock_org.list_date END,"
                " updated_at=excluded.updated_at", rows)
            con.commit()
            filled += len(rows)
        if (i // BATCH + 1) % 10 == 0 or i + BATCH >= len(need):
            print("  批次 %d/%d｜本批 %d｜累计补 %d｜%.1fs"
                  % (i // BATCH + 1, (len(need) + BATCH - 1) // BATCH,
                     len(rows), filled, time.time() - t0), flush=True)
        time.sleep(0.05)

    now_org = {r["code"]: (r["industry"] or "", r["industry_csrc"] or "")
               for r in con.execute("SELECT code, industry, industry_csrc FROM stock_org")}
    cov = sum(1 for c in codes if now_org.get(c, ("", ""))[0])
    cov_csrc = sum(1 for c in codes if now_org.get(c, ("", ""))[1])
    print("完成：补 %d 行｜stock_org 有行业 %d 只（%.2f%%）｜有证监会行业 %d 只（%.2f%%）"
          % (filled, cov, 100.0 * cov / max(1, len(codes)),
             cov_csrc, 100.0 * cov_csrc / max(1, len(codes))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
