#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手算复核：挑几个 (规则, 日期, H)，用**纯 SQL + Python 字典**从零重算当日超额，
再和引擎 `--dump-daily` 写出来的逐日数字对照。

这条路径刻意不复用引擎的任何数组代码（不用 numpy 矩阵、不用前向填充矩阵），
只用 SQL 取「下一根 K 线」「H 根之后的 K 线」，唯一共享的是判据公式的语义 ——
所以它能抓出对齐/平移/聚合类的错，抓不出「公式本身抄错」（那个由 selfcheck 的 A/B 抓）。

用法：
    python tools/backtest_oss.py --dump-daily --out viewer/logs/bt_qa.json
    python tools/backtest_oss_verify.py viewer/logs/bt_qa.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KBASE = ROOT / "viewer" / "kbase.db"
APP_DB = ROOT / "viewer" / "app.db"

CASES = (
    ("oss_burst", "2024-09-13", 5),
    ("oss_reversal20", "2024-10-09", 20),
    ("oss_near_high", "2025-07-15", 60),
    ("oss_ma_align", "2026-03-10", 40),
    ("oss_rsi_overbought", "2025-02-19", 5),
    ("oss_shrink_vol", "2026-06-17", 10),
    ("oss_mom20", "2025-04-09", 20),
)


def main(json_path: str) -> int:
    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    rows = {(r["rule"], r["horizon"]): r for r in payload["results"]
            if "daily" in r}
    if not rows:
        print("JSON 里没有逐日序列，请先用 --dump-daily 跑一遍")
        return 1

    con = sqlite3.connect("file:%s?mode=ro" % KBASE.as_posix(), uri=True)
    st = {str(r[0]) for r in sqlite3.connect(
        "file:%s?mode=ro" % APP_DB.as_posix(), uri=True).execute(
        "SELECT code FROM stock_names WHERE upper(name) LIKE '%ST%'")}

    problems = 0
    for rule, day, H in CASES:
        key = (rule, H)
        if key not in rows:
            print("跳过 %s T+%d（本次没跑这个组合）" % (rule, H))
            continue
        rec = rows[key]["daily"]
        if day not in rec["dates"]:
            print("跳过 %s @%s（不在本次窗口）" % (rule, day))
            continue
        i = rec["dates"].index(day)
        engine_daily = rec["excess"][i]
        engine_cnt = rec["count"][i]

        # ---- 纯 SQL 重算：当日全市场 + 命中票 ----
        until = payload["meta"]["window"]["until"]
        bars = con.execute(
            "SELECT code,date,c,h,l,v FROM kline WHERE date>=? AND date<=?"
            " AND c>0 ORDER BY code,date", ("2019-01-01", until)).fetchall()
        per: dict[str, list] = defaultdict(list)
        for code, d, c, h, l, v in bars:
            per[str(code)].append((d, float(c), float(h), float(l), float(v)))

        cal = [str(r[0]) for r in con.execute(
            "SELECT DISTINCT date FROM kline WHERE date>=? AND date<=? ORDER BY date",
            (day, payload["meta"]["window"]["until"]))]
        if len(cal) < H + 2:
            print("跳过 %s @%s（剩余日历不足 %d 天）" % (rule, day, H))
            continue
        entry_day, exit_day = cal[1], cal[H + 1]
        exit_day = cal[min(H + 1, len(cal) - 1)]

        rets: dict[str, float] = {}
        for code, ser in per.items():
            dmap = {x[0]: x for x in ser}
            if day not in dmap or entry_day not in dmap:
                continue
            # 平仓日：当天有就用当天，没有就顺延到之后第一个有行情的日子
            k = H + 1
            while k < len(cal) and cal[k] not in dmap:
                k += 1
            if k >= len(cal):
                continue
            rets[code] = (dmap[cal[k]][1] / dmap[entry_day][1] - 1.0) * 100.0

        # 基准：非 ST、成立满 60 个交易日
        ok = []
        for code, r in rets.items():
            if code in st:
                continue
            ser = per[code]
            if len(ser) < 60:
                continue
            ok.append(r)
        bench = sum(ok) / len(ok) if ok else float("nan")

        # 命中：用 SQL 路径独立实现当日判据
        hits: list[str] = []
        for code, ser in per.items():
            if day not in {x[0] for x in ser[-1:]} and ser[-1][0] != day:
                pass
            dmap = {x[0]: x for x in ser}
            if day not in dmap:
                continue
            if code not in rets or code in st:
                continue
            idx = [x[0] for x in ser].index(day)
            closes = [x[1] for x in ser[:idx + 1]]
            vols = [x[4] for x in ser[:idx + 1]]
            n = len(closes)
            if n < 60:
                continue

            def pct(a, b):
                return (a / b - 1.0) * 100.0 if b and b > 0 and a and a > 0 else None

            c = closes[-1]
            ret1 = pct(c, closes[-2] if n >= 2 else None)
            mom20 = pct(c, closes[-22] if n >= 22 else None)
            vr = (vols[-1] / (sum(vols[-5:]) / 5.0)) if sum(vols[-5:]) > 0 else None
            ma5 = sum(closes[-5:]) / 5.0
            ma20 = sum(closes[-20:]) / 20.0
            ma60 = sum(closes[-60:]) / 60.0
            hi = max(closes[-251:])
            if rule == "oss_burst":
                hit = ret1 is not None and ret1 >= 4.0
            elif rule == "oss_reversal20":
                hit = mom20 is not None and mom20 <= -20.0
            elif rule == "oss_near_high":
                hit = pct(c, hi) is not None and pct(c, hi) >= -25.0
            elif rule == "oss_ma_align":
                hit = c > ma5 > ma20 > ma60
            elif rule == "oss_rsi_overbought":
                # Wilder RSI6
                g = l = 0.0
                ag = al = 0.0
                first = True
                for k in range(1, n):
                    ch = closes[k] - closes[k - 1]
                    gg = max(ch, 0.0)
                    ll = max(-ch, 0.0)
                    if first:
                        ag, al, first = gg, ll, False
                    else:
                        ag = (ag * 5.0 + gg) / 6.0
                        al = (al * 5.0 + ll) / 6.0
                rsi = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
                hit = rsi >= 80.0
            else:
                hit = False
            if hit:
                hits.append(code)

        if not hits:
            print("跳过 %s @%s：手算命中 0 只" % (rule, day))
            continue
        mine = sum(rets[c] for c in hits) / len(hits) - bench - 0.25
        print("%-20s @%s T+%-3d 引擎 daily=%s (n=%d) | 手算 %+.3f (命中 %d / 基准 %+.2f%%)"
              % (rule, day, H,
                 "%.3f" % engine_daily if engine_daily is not None else "None",
                 engine_cnt, mine, len(hits), bench))
        if engine_daily is None or abs(mine - engine_daily) > 0.35:
            problems += 1
            print("   ★ 差异过大（容差 0.35pp，来源可能是 ST 表/次新口径的边界票）")
    con.close()
    print("\n结论：%d 个用例超容差" % problems)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else
                  "viewer/logs/backtest_oss_20261105_main.json"))
