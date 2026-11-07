# -*- coding: utf-8 -*-
"""决策台前向成绩单 —— 2026-11-05

## 为什么要有这支脚本

决策台的四条主线（`policy_funnel` / `t1_surf` / `dip_revert` / `resonance` + `self_learn`）
**没有历史可回测**：`pillar_item` 只从 2026-10-14 开始记录。之前我据此说「无法验证」，
这个说法是错的 —— 那只对「历史回测」成立。

回测有两条腿：

  ① 历史回测：用过去的数据重放判据。需要判据能从原始数据重建（K 线类可以，快照类不行）。
  ② 前向打分：从今天起把判断写下来，到期就用真实价格打分。

**第②条的滞后等于持有期，不是"一年"**：T+1 的工具 5 天后就有第一批分，T+5 的 10 天后。

而 `pillar_item` 已经在记录了（16 个交易日、487 条）。所以这支脚本做的第一件事，
就是把已有的 16 天**立刻打上 T+1 / T+3 / T+5 的分**，而不是等。

## 口径

- 进场价 = `run_date` 当日收盘（决策台收盘后出票，次日才能买；这里按收盘价算，
  是**偏乐观**的口径，等于假设你能在收盘瞬间成交。已知偏松，见报告局限）
- 基准 = 全市场等权指数同期（`viewer/logs/ew_index.json`，`tools/cond_box_market_base.py` 建的）
- 超额 = 主线组收益 − 基准，扣 0.25% 往返成本
- 同时给「逐票」与「逐日等权」两个口径：逐日等权不会被某一天票多的主线带偏

## 用法
  python tools/pillar_forward_score.py
  python tools/pillar_forward_score.py --holds 1,2,3,5,10
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "viewer" / "app.db"
QFQ = ROOT / "data" / "kline_daily.db"
OUT = ROOT / "viewer" / "logs"
EW = OUT / "ew_index.json"
COST = 0.0025


def load_ew() -> tuple[list[int], np.ndarray]:
    if not EW.exists():
        raise SystemExit("缺 %s，先跑 tools/cond_box_market_base.py --rebuild" % EW)
    d = json.loads(EW.read_text(encoding="utf-8"))
    return [int(x) for x in d["dates"]], np.array(d["index"], dtype=np.float64)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holds", default="1,2,3,5")
    ap.add_argument("--stamp", default="20261105")
    args = ap.parse_args()
    holds = [int(x) for x in args.holds.split(",") if x.strip()]

    ew_dates, ew_idx = load_ew()
    ewp = {d: i for i, d in enumerate(ew_dates)}

    con = sqlite3.connect(str(APP))
    rows = con.execute(
        "SELECT run_date,pillar,symbol,rank,score FROM pillar_item ORDER BY run_date,pillar,rank"
    ).fetchall()
    con.close()
    if not rows:
        print("pillar_item 是空的")
        return
    dates = sorted({r[0] for r in rows})
    print("决策台记录：%d 条，%d 个交易日（%s ~ %s）"
          % (len(rows), len(dates), dates[0], dates[-1]))

    q = sqlite3.connect(str(QFQ))
    px_cache: dict[str, tuple[list[int], dict[int, float]]] = {}
    for sym in {r[2] for r in rows}:
        rs = q.execute("SELECT d,c FROM kline WHERE code=? ORDER BY d", (sym,)).fetchall()
        px_cache[sym] = ([int(x[0]) for x in rs], {int(x[0]): float(x[1] or 0) for x in rs})
    q.close()

    # 逐票明细
    det: list[dict] = []
    for run_date, pillar, sym, rank, score in rows:
        d0 = int(str(run_date).replace("-", ""))
        ds, px = px_cache.get(sym, ([], {}))
        if d0 not in px:
            continue
        i0 = ds.index(d0)
        ei = ewp.get(d0)
        rec = {"run_date": run_date, "pillar": pillar, "code": sym,
               "rank": int(rank or 0), "score": float(score or 0)}
        for H in holds:
            if i0 + H >= len(ds):
                continue
            c0, c1 = px[ds[i0]], px[ds[i0 + H]]
            if c0 <= 0 or c1 <= 0:
                continue
            rec["r%d" % H] = (c1 / c0 - 1.0 - COST) * 100.0
            if ei is not None and ei + H < len(ew_idx):
                rec["b%d" % H] = (ew_idx[ei + H] / ew_idx[ei] - 1.0) * 100.0
        if any(("r%d" % H) in rec for H in holds):
            det.append(rec)

    if not det:
        print("没有可打分的记录")
        return

    # 汇总
    report: dict[str, dict] = {}
    by_pillar: dict[str, list[dict]] = defaultdict(list)
    for x in det:
        by_pillar[x["pillar"]].append(x)
    by_pillar["__ALL__"] = det

    for pillar, items in by_pillar.items():
        rep = {}
        for H in holds:
            rk, bk = "r%d" % H, "b%d" % H
            pairs = [(x[rk], x.get(bk)) for x in items if rk in x]
            if len(pairs) < 3:
                continue
            r = np.array([p[0] for p in pairs])
            ex = np.array([p[0] - p[1] for p in pairs if p[1] is not None])
            # 逐日等权（同一天多只票先平均，避免某天票多带偏）
            per_day: dict[str, list[float]] = defaultdict(list)
            for x in items:
                if rk in x and (rk, "b") and x.get(bk) is not None:
                    per_day[x["run_date"]].append(x[rk] - x[bk])
            dm = [float(np.mean(v)) for v in per_day.values() if v]
            rep["T+%d" % H] = {
                "n_picks": int(len(r)),
                "n_days": int(len(dm)),
                "mean_pct": round(float(r.mean()), 3),
                "excess_pct": round(float(ex.mean()), 3) if len(ex) else None,
                "excess_day_eq_pct": round(float(np.mean(dm)), 3) if dm else None,
                "win_pct": round(float((ex > 0).mean()) * 100, 1) if len(ex) else None,
            }
        if rep:
            report[pillar] = rep

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / ("pillar_fwd_%s.json" % args.stamp)
    path.write_text(json.dumps({"meta": {"holds": holds, "n_rows": len(det),
                                         "dates": dates, "cost": COST,
                                         "bench": "ew_index", "entry": "run_date close"},
                                "report": report}, ensure_ascii=False, indent=1),
                    encoding="utf-8")

    print("\n== 决策台前向成绩单（进场=出票日收盘，扣 0.25%% 往返，基准=全市场等权）==")
    print("   逐票超额 = 每只票减各自持有期的基准；逐日 = 先按日平均再加权，防某天票多带偏")
    hdr = "%-14s %-5s %6s %6s %9s %10s %10s %7s" % (
        "主线", "持有", "票数", "天数", "绝对%", "逐票超额pp", "逐日超额pp", "胜率%")
    print(hdr)
    order = ["__ALL__", "policy_funnel", "t1_surf", "dip_revert", "resonance", "self_learn", "quant_self"]
    for pillar in order + [p for p in report if p not in order]:
        if pillar not in report:
            continue
        for H in holds:
            r = report[pillar].get("T+%d" % H)
            if not r:
                continue
            name = "全部主线" if pillar == "__ALL__" else pillar
            print("%-14s T+%-3d %6d %6d %+9.3f %+10s %+10s %7s"
                  % (name, H, r["n_picks"], r["n_days"], r["mean_pct"],
                     "n/a" if r["excess_pct"] is None else "%.3f" % r["excess_pct"],
                     "n/a" if r["excess_day_eq_pct"] is None else "%.3f" % r["excess_day_eq_pct"],
                     "n/a" if r["win_pct"] is None else "%.1f" % r["win_pct"]))
    print("\n写盘：%s" % path)


if __name__ == "__main__":
    main()
