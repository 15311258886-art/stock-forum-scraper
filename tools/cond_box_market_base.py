# -*- coding: utf-8 -*-
"""给条件化回测配一条「全市场等权基准」—— 2026-11-05

## 为什么单独一支脚本

池内区分力（触发组 − 同池对照组）只回答「池子里谁更好」。还剩一个问题没答：
**箱体池本身值不值得看？** 也就是这批票相对「随便买一篮子 A 股」是强还是弱。
之前那套「绝对超额」口径算的就是这件事，只是它被拿去评价工具的好坏了 —— 那是
拿错了尺子；但作为**背景信息**它必须留着，否则会误读池内结果。

## 怎么算

等权指数：每个交易日 r_d = 当天与前一天都有收盘价的票的日收益均值，逐日累乘。
用「当天也在、前一天也在」的票算，是为了不受停牌/新上市/退市的成分变动干扰。
H 日等权收益 = I[d+H] / I[d] − 1。

指数算一次就缓存到 viewer/logs/ew_index.json，之后只读缓存。

## 用法
  python tools/cond_box_market_base.py                      # 读 cond_box_20261105_long.json
  python tools/cond_box_market_base.py --stamp xxx
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
QFQ = ROOT / "data" / "kline_daily.db"
OUT = ROOT / "viewer" / "logs"
CACHE = OUT / "ew_index.json"
COST = 0.0025


def build_index(force: bool = False) -> tuple[list[int], np.ndarray]:
    """建等权指数。返回 (dates, idx)，idx[i] 是第 i 天的净值（起点=1）。"""
    if CACHE.exists() and not force:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        return [int(x) for x in d["dates"]], np.array(d["index"], dtype=np.float64)

    con = sqlite3.connect(str(QFQ))
    dates = [int(r[0]) for r in con.execute(
        "SELECT DISTINCT d FROM kline WHERE d>=20080101 ORDER BY d")]
    pos = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    sumr = np.zeros(n)
    cnt = np.zeros(n)
    codes = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT code FROM kline WHERE d>=20080101")]
    for j, code in enumerate(codes):
        rows = con.execute("SELECT d,c FROM kline WHERE code=? AND d>=? ORDER BY d",
                           (code, 20080101)).fetchall()
        if len(rows) < 3:
            continue
        dd = np.array([r[0] for r in rows], dtype=np.int64)
        cc = np.array([r[1] or 0.0 for r in rows], dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            ret = np.where((cc[1:] > 0) & (cc[:-1] > 0), cc[1:] / cc[:-1] - 1.0, np.nan)
        ok = np.isfinite(ret)
        if not ok.any():
            continue
        idx = np.array([pos[int(x)] for x in dd[1:]], dtype=np.int64)
        sumr += np.bincount(idx[ok], weights=ret[ok], minlength=n)
        cnt += np.bincount(idx[ok], minlength=n)
        if (j + 1) % 1500 == 0:
            print("  指数构建：%d/%d 只" % (j + 1, len(codes)))
    con.close()

    r = np.where(cnt > 0, sumr / np.maximum(cnt, 1.0), 0.0)
    idxv = np.cumprod(1.0 + r)
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"dates": dates, "index": [round(float(x), 8) for x in idxv],
                                 "n": int(len(dates))}), encoding="utf-8")
    return dates, idxv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="20261105_long")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    dates, idxv = build_index(force=args.rebuild)
    dpos = {d: i for i, d in enumerate(dates)}
    print("等权指数：%d 个交易日 %d ~ %d，期末净值 %.3f"
          % (len(dates), dates[0], dates[-1], idxv[-1]))

    src = OUT / ("cond_box_%s.json" % args.stamp)
    if not src.exists():
        print("找不到 %s，先跑 cond_box_backtest.py" % src.name)
        return
    payload = json.loads(src.read_text(encoding="utf-8"))
    report = payload["report"]
    days_by = payload.get("days_by_hold") or {}

    print("\n== 箱体池 vs 全市场等权（同日期、同持有期，含 0.25% 往返成本）==")
    print("   口径：池 = 通过箱体一级+二级门槛的票；差 = 池均值 − 指数同期，单位 pp")
    rows_out = {}
    for key in sorted(report, key=lambda k: int(k.split("+")[1])):
        H = int(key.split("+")[1])
        r = report[key]
        days = days_by.get(str(H)) or []
        if not days or not r.get("n_days"):
            continue
        mkt = []
        for d in days:
            i = dpos.get(int(d))
            if i is None or i + H >= len(idxv):
                continue
            mkt.append(idxv[i + H] / idxv[i] - 1.0)
        if not mkt:
            continue
        mkt = np.array(mkt)
        pool = float(r["pool_mean_pct"]) / 100.0
        mmean = float(mkt.mean())
        rows_out[key] = {"pool_pct": round(pool * 100, 3),
                         "market_pct": round(mmean * 100, 3),
                         "excess_pct": round((pool - mmean) * 100, 3),
                         "n_days": int(len(mkt))}
        print("%-6s 池均值 %+.3f%% | 指数 %+.3f%% | 超额 %+.3fpp | 样本 %d 日"
              % (key, pool * 100, mmean * 100, (pool - mmean) * 100, len(mkt)))

    payload["market_base"] = rows_out
    src.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已回写 %s" % src.name)


if __name__ == "__main__":
    main()
