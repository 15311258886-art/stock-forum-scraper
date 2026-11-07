#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后复权库除权缺口审计（kbase.kline 'hfq' vs data/kline_daily.db 前复权）。

为什么需要这个脚本
------------------
课题最初要求「一律用 kbase 的后复权」，理由是后复权不会因为将来除权而改写历史。
但主理人用**逐票全序列日收益**比对后发现：kbase 标称 hfq，却在**除权日**存在机械缺口
（例：605100 于 2026-05-25 从 55.36 跳到 39.55，−28.56%，而前复权库同日 +0.02%）。
不是复权方式之争，是 kbase 的复权因子在某些除权事件上没打上。

于是必须回答两个问题：
  1. 缺口有多大范围（多少股票日、多少只票、偏差分位）；
  2. 这会不会把回测结论带偏。

本脚本只做 1（2 由 tools/cascade_delta.py 的 Step 6 用前复权重建来回答）。
另外顺手否证一个**看似合理但无效**的检查方法，写下来免得再有人走一遍：

  ✗ 单日比值法：`stock_indicators_daily.close ÷ kline.c`。
    后复权序列的**最后一根**就等于真实成交价（基准在最早期），前复权也锚在最新日，
    所以最新日两边必然相等 —— 这个比值为 1 是**构造决定的**，验不出任何历史差异。
    （我第一版就是这么验的，p1=p99=min=max=1.0000，看着极强，实际什么也没验。）

  ✓ 逐票全序列日收益法：同一只票、同一个相邻交易日对，两边各自算日收益再相减。
    只有这个能落在除权日上。

用法：
    python tools/audit_hfq_gap.py                       # 主窗口 2023-11-06~2026-11-04
    python tools/audit_hfq_gap.py --since 2024-02-07
    python tools/audit_hfq_gap.py --json viewer/logs/xxx.json

★ 只读两个库，不写库、不改 viewer/ 下任何文件。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KBASE = ROOT / "viewer" / "kbase.db"
QFQ = ROOT / "data" / "kline_daily.db"

try:
    import numpy as np
except ImportError:                                       # pragma: no cover
    sys.exit("需要 numpy：请用 .venv/Scripts/python.exe 运行")

GAP_PCT = 0.5          # |日收益差| 超过这个百分点就算一次缺口（主理人用的阈值）
TOP_N = 12


def _y8(d: str) -> int:
    return int(d.replace("-", ""))


def load_series(db: Path, lo: str, hi: str, date_col: str, int_date: bool) -> dict[str, tuple]:
    """code → (dates, close) 两个 numpy 数组。整库进内存约 60MB，比逐票查库快得多。"""
    a = _y8(lo) if int_date else lo
    b = _y8(hi) if int_date else hi
    sql = ("SELECT code,%s,c FROM kline WHERE %s>=? AND %s<=? AND c>0 ORDER BY code,%s"
           % (date_col, date_col, date_col, date_col))
    out: dict[str, tuple[list, list]] = {}
    con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    for code, dt, c in con.execute(sql, (a, b)):
        code = str(code)
        if code not in out:
            out[code] = ([], [])
        out[code][0].append(str(dt) if int_date else str(dt))
        out[code][1].append(float(c))
    con.close()
    return {k: (np.array(v[0]), np.array(v[1], dtype=np.float64)) for k, v in out.items()}


def daily_returns(s: tuple) -> tuple[np.ndarray, np.ndarray]:
    """(dates, close) → (dates[1:], 日收益数组)。注意日期是**后一天**的日期。"""
    dates, c = s
    if dates.size < 2:
        return np.array([], dtype=object), np.array([], dtype=np.float64)
    prev, cur = c[:-1], c[1:]
    ok = (prev > 0) & (cur > 0)
    return dates[1:][ok], (cur[ok] / prev[ok] - 1.0) * 100.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2023-11-06")
    ap.add_argument("--until", default="2026-11-04")
    ap.add_argument("--gap", type=float, default=GAP_PCT)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)

    if not KBASE.exists() or not QFQ.exists():
        sys.exit("缺少库：%s / %s" % (KBASE, QFQ))

    print("载入 kbase 后复权 …", flush=True)
    K = load_series(KBASE, a.since, a.until, "date", int_date=False)
    print("载入 kline_daily 前复权 …", flush=True)
    Q = load_series(QFQ, a.since, a.until, "d", int_date=True)

    # 前复权库的日期是 20231106 形式，统一成 2023-11-06 再对齐
    def _norm(d: str) -> str:
        return "%s-%s-%s" % (d[:4], d[4:6], d[6:8]) if len(d) == 8 and d.isdigit() else d

    Q = {k: (np.array([_norm(x) for x in v[0]]), v[1]) for k, v in Q.items()}

    codes = sorted(set(K) & set(Q))
    both_stockdays = 0
    gap_days = 0
    gap_codes: set[str] = set()
    devs: list[float] = []
    events: list[tuple[float, str, str, float, float]] = []
    name_only_k = set(K) - set(Q)
    name_only_q = set(Q) - set(K)

    for code in codes:
        kd, kr = daily_returns(K[code])
        qd, qr = daily_returns(Q[code])
        if kd.size == 0 or qd.size == 0:
            continue
        km = {d: i for i, d in enumerate(kd)}
        qm = {d: i for i, d in enumerate(qd)}
        common = [d for d in kd if d in qm]
        if not common:
            continue
        ki = np.array([km[d] for d in common])
        qi = np.array([qm[d] for d in common])
        diff = kr[ki] - qr[qi]
        both_stockdays += diff.size
        bad = np.abs(diff) > a.gap
        if bad.any():
            gap_days += int(bad.sum())
            gap_codes.add(code)
            devs.extend(np.abs(diff[bad]).tolist())
            for j in np.where(bad)[0]:
                events.append((abs(float(diff[j])), code, common[j],
                               float(kr[ki[j]]), float(qr[qi[j]])))

    devs_a = np.array(devs) if devs else np.array([0.0])
    events.sort(reverse=True)
    payload = {
        "window": {"since": a.since, "until": a.until},
        "threshold_pct": a.gap,
        "universe": {"kbase_codes": len(K), "qfq_codes": len(Q), "common_codes": len(codes),
                     "only_in_kbase": len(name_only_k), "only_in_qfq": len(name_only_q)},
        "stockdays_compared": both_stockdays,
        "gap_stockdays": gap_days,
        "gap_rate_pct": round(100.0 * gap_days / both_stockdays, 4) if both_stockdays else None,
        "gap_codes": len(gap_codes),
        "gap_code_rate_pct": round(100.0 * len(gap_codes) / len(codes), 2) if codes else None,
        "abs_dev_quantiles_pct": {
            "p50": round(float(np.percentile(devs_a, 50)), 3),
            "p90": round(float(np.percentile(devs_a, 90)), 3),
            "p99": round(float(np.percentile(devs_a, 99)), 3),
            "max": round(float(devs_a.max()), 3),
        },
        "top_events": [
            {"code": c, "date": d, "kbase_ret_pct": round(kr_, 3), "qfq_ret_pct": round(qr_, 3),
             "dev_pp": round(abs(kr_ - qr_), 3)}
            for _, c, d, kr_, qr_ in events[:TOP_N]
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if a.json:
        p = Path(a.json)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n落盘 %s" % p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
