#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""限售解禁信号的实证探针（2026-10-30）。

进哥的假设：新股 / 次新股的股东临近解禁，可以变现 → 容易砸盘 → 应提前提示风险。

**不预设结论。** 本脚本只回答三件事：
  1) 解禁事件前后，T+5 / T+20 相对全市场超额是多少，显著吗？
  2) 分档看：解禁占流通比 / 解禁类型 / 股东数 / 是否次新股，哪一档才是真信号？
  3) 是不是「解禁前先跌」（可提前避）还是「解禁后才跌」（只能事后知道）？

口径与前几个探针一致，便于横向比较：
  · 超额 = 个股收益 − 同日全市场均值（demean，剔除大盘 beta）
  · t 值先按「事件日」聚合成一个日序列再检验；不这么做，同一天几百只解禁票
    会被当成几百个独立样本，t 值虚高（这是最常见的伪显著来源）
  · 数据源：东财 datacenter RPT_LIFT_STAGE（限售解禁批次），本地 K 线算收益

用法：
  python tools/probe_lift_signal.py                     # 默认 2025-08-18 起（K线起点）
  python tools/probe_lift_signal.py --start 2025-09-01
  python tools/probe_lift_signal.py --fetch-only        # 只拉数据看覆盖量，不跑统计
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.parse
import urllib.request
from bisect import bisect_left
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "viewer", "app.db")

API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 解禁类型 → 粗分类。原始 FREE_SHARES_TYPE 有十几种，但对抛压含义差别很大：
#   定增机构配售：机构拿票成本低，到期减持动机最强
#   股权激励：量通常很小，且核心人员减持受限
#   首发原股东：大股东受减持新规约束（破发/破净/分红不达标不得减）
def type_group(t: str) -> str:
    t = t or ""
    if "定向增发" in t:
        return "定增解禁"
    if "股权激励" in t or "激励" in t:
        return "股权激励"
    if "首发" in t or "原股东" in t or "战略配售" in t:
        return "首发原股东"
    return "其他"


def fetch(start: str, end: str, page_size: int = 500) -> list[dict]:
    """拉取 [start, end] 区间内所有解禁批次。分页直到取完。"""
    out: list[dict] = []
    page = 1
    while True:
        filt = "(FREE_DATE>='%s')(FREE_DATE<='%s')" % (start, end)
        q = urllib.parse.urlencode({
            "reportName": "RPT_LIFT_STAGE",
            "columns": "ALL",
            "pageNumber": page,
            "pageSize": page_size,
            "sortColumns": "FREE_DATE,SECURITY_CODE",
            "sortTypes": "1,1",
            "filter": filt,
            "source": "WEB",
            "client": "WEB",
        })
        req = urllib.request.Request(API + "?" + q, headers={
            "User-Agent": UA, "Referer": "https://data.eastmoney.com/"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                js = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print("  [warn] 第 %d 页失败: %s" % (page, e))
            break
        res = js.get("result") or {}
        data = res.get("data") or []
        if not data:
            break
        out.extend(data)
        pages = res.get("pages") or 1
        if page >= pages:
            break
        page += 1
        time.sleep(0.25)          # 别把对方打急
    return out


def ensure_table(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS lift_event (
            code        TEXT NOT NULL,
            name        TEXT,
            free_date   TEXT NOT NULL,      -- 解禁日 YYYY-MM-DD
            share_type  TEXT,               -- 原始解禁类型
            type_group  TEXT,               -- 粗分类
            n_holder    INTEGER,            -- 本批解禁股东数
            free_shares REAL,               -- 本次解禁股数（万股）
            lift_cap    REAL,               -- 解禁市值（万元）
            free_ratio  REAL,               -- 占流通股比例
            total_ratio REAL,               -- 占总股本比例
            is_new      INTEGER,            -- 是否新股
            fetched_at  TEXT,
            PRIMARY KEY (code, free_date, share_type)
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_lift_date ON lift_event(free_date)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_lift_code ON lift_event(code)")
    con.commit()


def save(con: sqlite3.Connection, rows: list[dict]) -> int:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for r in rows:
        fd = (r.get("FREE_DATE") or "")[:10]
        code = r.get("SECURITY_CODE")
        if not (code and fd):
            continue
        try:
            con.execute(
                "INSERT OR REPLACE INTO lift_event"
                " (code,name,free_date,share_type,type_group,n_holder,free_shares,"
                "  lift_cap,free_ratio,total_ratio,is_new,fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, r.get("SECURITY_NAME_ABBR"), fd,
                 r.get("FREE_SHARES_TYPE"), type_group(r.get("FREE_SHARES_TYPE")),
                 r.get("BATCH_HOLDER_NUM"), r.get("CURRENT_FREE_SHARES"),
                 r.get("LIFT_MARKET_CAP"), r.get("FREE_RATIO"),
                 r.get("TOTAL_RATIO") or r.get("TOTALSHARES_RATIO"),
                 1 if str(r.get("IS_NEW")) == "1" else 0, now))
            n += 1
        except sqlite3.Error:
            continue
    con.commit()
    return n


def tstat(vals: list[float]) -> float | None:
    n = len(vals)
    if n < 5:
        return None
    m = statistics.fmean(vals)
    s = statistics.stdev(vals)
    return None if s == 0 else m / (s / n ** 0.5)


def load_market(con: sqlite3.Connection, start: str):
    """一次性取 close 序列 + 前向 5/20 日收益，并按日算全市场均值用于 demean。"""
    rows = con.execute("""
        SELECT code, date, close,
               LEAD(close,5)  OVER (PARTITION BY code ORDER BY date) f5,
               LEAD(close,20) OVER (PARTITION BY code ORDER BY date) f20,
               LAG(close,10)  OVER (PARTITION BY code ORDER BY date) l10,
               LAG(close,20)  OVER (PARTITION BY code ORDER BY date) l20
        FROM stock_indicators_daily WHERE date >= ?
    """, (start,)).fetchall()
    f5, f20, b10, b20 = {}, {}, {}, {}
    d5, d20, db10, db20 = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for code, d, c, v5, v20, l10, l20 in rows:
        if not c or c <= 0:
            continue
        if v5 and v5 > 0:
            r = (v5 / c - 1) * 100
            f5[(code, d)] = r
            d5[d].append(r)
        if v20 and v20 > 0:
            r = (v20 / c - 1) * 100
            f20[(code, d)] = r
            d20[d].append(r)
        if l10 and l10 > 0:
            r = (c / l10 - 1) * 100
            b10[(code, d)] = r
            db10[d].append(r)
        if l20 and l20 > 0:
            r = (c / l20 - 1) * 100
            b20[(code, d)] = r
            db20[d].append(r)
    m5 = {k: statistics.fmean(v) for k, v in d5.items()}
    m20 = {k: statistics.fmean(v) for k, v in d20.items()}
    mb10 = {k: statistics.fmean(v) for k, v in db10.items()}
    mb20 = {k: statistics.fmean(v) for k, v in db20.items()}
    dates = sorted(d5)
    return dict(f5=f5, f20=f20, b10=b10, b20=b20,
                m5=m5, m20=m20, mb10=mb10, mb20=mb20, dates=dates)


def align(cal: list[str], ymd: str) -> int | None:
    """解禁日 → 当日或之后第一个交易日（解禁日常落在非交易日）。"""
    i = bisect_left(cal, ymd[:10])
    return i if i < len(cal) else None


def bucket_of(v, edges):
    if v is None:
        return None
    for lo, hi, name in edges:
        if lo <= v < hi:
            return name
    return None


def report(con: sqlite3.Connection, start: str) -> int:
    mk = load_market(con, start)
    cal = mk["dates"]
    print("交易日 %d 个：%s .. %s" % (len(cal), cal[0], cal[-1]))

    events = con.execute(
        "SELECT code,name,free_date,type_group,n_holder,lift_cap,free_ratio,"
        "total_ratio,share_type,is_new FROM lift_event WHERE free_date>=? "
        "ORDER BY free_date", (start,)).fetchall()
    print("解禁批次 %d 条" % len(events))

    # 按 (code, trade_date) 去重：同一天同一票多批解禁只算一次事件，避免重复计权
    ev = {}
    for (code, name, fd, tg, nh, cap, fr, tr, st, isnew) in events:
        i = align(cal, fd)
        if i is None:
            continue
        d = cal[i]
        k = (code, d)
        if k in ev:
            old = ev[k]
            old["cap"] = (old["cap"] or 0) + (cap or 0)
            old["tr"] = (old["tr"] or 0) + (tr or 0)
            continue
        ev[k] = dict(code=code, name=name, date=d, tg=tg, nh=nh, cap=cap,
                     fr=fr, tr=tr, st=st, isnew=isnew)
    print("对齐到交易日后 %d 个事件" % len(ev))

    # 只保留能算收益的
    usable = [(k, e) for k, e in ev.items() if k in mk["f5"]]
    print("可算 T+5 的 %d 个（%.0f%%）" % (len(usable), 100.0 * len(usable) / max(1, len(ev))))

    def agg(sub, key, tag):
        """按事件日聚合成日序列后求 t，避免同日多票造成伪显著。"""
        vals = []
        byd = defaultdict(list)
        for k, e in sub:
            v = mk[key].get(k)
            if v is None:
                continue
            base = {"f5": "m5", "f20": "m20", "b10": "mb10", "b20": "mb20"}[key]
            ex = v - mk[base].get(e["date"], 0.0)
            vals.append(ex)
            byd[e["date"]].append(ex)
        if len(vals) < 30:
            print("  %-22s n=%-5d (样本不足)" % (tag, len(vals)))
            return
        dv = [statistics.fmean(v) for v in byd.values() if len(v) >= 3]
        t = tstat(dv)
        win = 100.0 * sum(1 for x in vals if x > 0) / len(vals)
        mark = ""
        if t is not None:
            if t <= -2.0:
                mark = " ★显著负"
            elif t <= -1.3:
                mark = " ◇轻微负"
            elif t >= 2.0:
                mark = " ☆显著正"
        print("  %-22s n=%-5d 日数%-4d 超额%+7.2f%%  t=%7s  胜率%4.1f%%%s"
              % (tag, len(vals), len(dv), statistics.fmean(vals),
                 ("%.2f" % t) if t is not None else "-", win, mark))

    print("\n[1] 解禁事件整体：解禁日当天的前后表现")
    agg(usable, "b20", "解禁前20日")
    agg(usable, "b10", "解禁前10日")
    agg(usable, "f5", "解禁后T+5")
    agg(usable, "f20", "解禁后T+20")

    print("\n[2] 按「占流通股比例」分档（抛压大小）—— T+5 超额")
    edges = [(0, 0.01, "<1%"), (0.01, 0.03, "1~3%"), (0.03, 0.10, "3~10%"),
             (0.10, 0.30, "10~30%"), (0.30, 99, ">30%")]
    for lo, hi, name in edges:
        sub = [(k, e) for k, e in usable if e["fr"] is not None and lo <= e["fr"] < hi]
        agg(sub, "f5", "占流通 " + name)
    print("\n    同档看 T+20：")
    for lo, hi, name in edges:
        sub = [(k, e) for k, e in usable if e["fr"] is not None and lo <= e["fr"] < hi]
        agg(sub, "f20", "占流通 " + name)

    print("\n[3] 按「解禁类型」—— T+5 超额（进哥关心的定增/首发重点看）")
    for tg in ("定增解禁", "首发原股东", "股权激励", "其他"):
        sub = [(k, e) for k, e in usable if e["tg"] == tg]
        agg(sub, "f5", tg)
    print("\n    同类型看 T+20：")
    for tg in ("定增解禁", "首发原股东", "股权激励", "其他"):
        sub = [(k, e) for k, e in usable if e["tg"] == tg]
        agg(sub, "f20", tg)

    print("\n[4] 交叉：定增解禁 × 占流通比例（最可能的真信号所在）")
    for lo, hi, name in edges:
        sub = [(k, e) for k, e in usable
               if e["tg"] == "定增解禁" and e["fr"] is not None and lo <= e["fr"] < hi]
        agg(sub, "f5", "定增+" + name)

    print("\n[5] 按解禁市值分档（绝对抛压 = 要卖多少钱）")
    cap_edges = [(0, 5000, "<0.5亿"), (5000, 20000, "0.5~2亿"),
                 (20000, 50000, "2~5亿"), (50000, 200000, "5~20亿"),
                 (200000, 9e9, ">20亿")]
    for lo, hi, name in cap_edges:
        sub = [(k, e) for k, e in usable if e["cap"] is not None and lo <= e["cap"] < hi]
        agg(sub, "f5", "解禁市值 " + name)

    print("\n[6] 是否次新股（进哥提的「上新时间不长」）")
    for flag, tag in ((1, "新股/次新"), (0, "老股")):
        sub = [(k, e) for k, e in usable if (e["isnew"] or 0) == flag]
        agg(sub, "f5", tag + " T+5")
        agg(sub, "f20", tag + " T+20")

    print("\n[7] 稳健性：时间对半切（前后半段必须同号才算真信号）")
    days = sorted({e["date"] for _, e in usable})
    if len(days) >= 20:
        mid = days[len(days) // 2]
        for half, tag in ((lambda d: d < mid, "前半段"), (lambda d: d >= mid, "后半段")):
            sub = [(k, e) for k, e in usable if half(e["date"])]
            agg(sub, "f5", tag + " T+5")
        print("    切点 = %s" % mid)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-08-18", help="实证起点（K线可用起点）")
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="重新拉取（默认增量）")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    ensure_table(con)

    today = time.strftime("%Y-%m-%d")
    # 一次拉：历史（做实证）+ 未来 12 个月（做提前提示）
    future_end = time.strftime("%Y-%m-%d", time.localtime(time.time() + 400 * 86400))
    print("[fetch] %s .. %s" % (args.start, future_end))
    rows = fetch(args.start, future_end)
    print("[fetch] 拿到 %d 条" % len(rows))
    n = save(con, rows)
    print("[save] 入库 %d 条" % n)

    tot = con.execute("SELECT COUNT(*) FROM lift_event").fetchone()[0]
    fut = con.execute("SELECT COUNT(*) FROM lift_event WHERE free_date>?",
                      (today,)).fetchone()[0]
    print("[db] 累计 %d 条，其中未来 %d 条" % (tot, fut))
    print("\n未来 90 天内解禁批次最多的 8 只：")
    q = """SELECT code,name,free_date,type_group,free_ratio,lift_cap
           FROM lift_event WHERE free_date BETWEEN ? AND date(?, '+90 day')
           ORDER BY lift_cap DESC LIMIT 8"""
    for r in con.execute(q, (today, today)):
        print("  %-8s %-8s %s %-8s 占流通%-7s 市值%.1f亿"
              % (r[0], (r[1] or "")[:8], r[2], r[3] or "-",
                 ("%.2f%%" % (r[4] * 100)) if r[4] is not None else "-",
                 (r[5] or 0) / 10000.0))
    con.close()

    if args.fetch_only:
        return 0
    con = sqlite3.connect(DB)
    report(con, max(args.start, "2025-08-18"))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
