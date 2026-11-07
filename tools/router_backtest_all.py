# -*- coding: utf-8 -*-
"""T10 · 批量工具回测：一次跑完一批工具，落 JSON + 汇总表。

与 `router_backtest.py` 的分工：那个是单工具 CLI（调参、排查用），
这个是「照着一张表跑完」，输出可以直接贴进报告的那种汇总。

用法
    # 纯 K 线类 7 个工具，5 年，T+20
    python tools/router_backtest_all.py --group kline --horizon 20 \
        --start 2021-11-08 --end 2026-11-06 --step 5

    # oss 系列 11 个（走历史指标表，区间受 stock_indicators_daily 限制）
    python tools/router_backtest_all.py --group oss --horizon 20 --step 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import router_backtest as RB  # noqa: E402
from viewer.kernel import params as _P  # noqa: E402  单一成本事实源（P4 统一 15bp）


# 纯 K 线判据：喂「截止 D 的日线窗口」就能判，口径与工具箱完全一致
GROUP_KLINE = [
    "box_bottom",
    "factor:tv_shrink_pull",
    "factor:tv_wbottom",
    "factor:tv_platform",
    "factor:tv_macd_div",
    "factor:db_wave",
    "factor:db_chan",
]

# 形态规则（oss 系列）：依赖指标表字段，走 oss 专用引擎（见 router_backtest_oss.py）
GROUP_OSS = [
    "oss_ma_align", "oss_near_high", "oss_near_high_tight",
    "oss_burst_vol", "oss_burst", "oss_pullback_ma20",
    "oss_shrink_vol", "oss_mom20", "oss_reversal20",
    "oss_high_vol", "oss_rsi_overbought",
]


def fmt_table(rows: list, horizon: int, cost_bp: float = 15.0) -> str:
    """把结果排成一张能贴进报告的 markdown 表。"""
    out = ["| 工具 | 截面 | 入选率 | 超额(pp) | 胜率 | t | 判定 |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        if not r.get("ok"):
            out.append("| `%s` | — | — | — | — | — | 跳过：%s |"
                       % (r.get("tool"), str(r.get("error") or "")[:40]))
            continue
        o = r["overall"]
        ex = o.get("excess_pp")
        t = o.get("t")
        if ex is None:
            verdict = "无入选"
        elif t is not None and abs(t) >= 2 and ex > 0:
            verdict = "**正向显著**"
        elif t is not None and abs(t) >= 2 and ex < 0:
            verdict = "**反向显著**（只能反向/排除用）"
        elif ex > 0:
            verdict = "正向不显著"
        else:
            verdict = "无 alpha"
        out.append("| `%s` | %d | %.1f%% | %s | %s | %s | %s |" % (
            r["tool"], o["sections"], o["pct_kept"],
            "—" if ex is None else ("%+.2f" % ex),
            "—" if o.get("win_rate") is None else ("%.1f%%" % o["win_rate"]),
            "—" if t is None else ("%.2f" % t),
            verdict))
    out.append("")
    out.append("（T+%d，已扣 %dbp 双边成本，超额基准 = 同时点同池）"
               % (horizon, int(round(cost_bp))))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="kline", choices=("kline", "oss"))
    ap.add_argument("--tools", default="", help="逗号分隔，覆盖 --group")
    ap.add_argument("--start", default="2021-11-08")
    ap.add_argument("--end", default="2026-11-06")
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--cost-bp", type=float,
                     default=round(_P.BACKTEST_FEE_ROUNDTRIP * 10000.0, 2),
                     help="双边成本(bp)，默认取内核统一值 15bp（params.BACKTEST_FEE_ROUNDTRIP）")
    ap.add_argument("--pool", default="all")
    ap.add_argument("--outdir", default="viewer/logs/backtest")
    a = ap.parse_args(argv)

    tools = [t.strip() for t in a.tools.split(",") if t.strip()] or (
        GROUP_KLINE if a.group == "kline" else GROUP_OSS)
    os.makedirs(a.outdir, exist_ok=True)
    pool = 0 if str(a.pool).lower() == "all" else int(a.pool)

    rows, t_all = [], time.time()
    for i, t in enumerate(tools, 1):
        print("\n=== [%d/%d] %s ===" % (i, len(tools), t))
        if a.group == "oss":
            from router_backtest_oss import run_oss
            r = run_oss(t, a.start, a.end, a.step, a.horizon, a.cost_bp, pool)
        else:
            r = RB.run(t, a.start, a.end, a.step, pool, a.horizon, a.cost_bp, 800)
        r["tool"] = t
        rows.append(r)
        if r.get("ok"):
            o = r["overall"]
            print("   → 入选率 %.1f%% / 超额 %s pp / 胜率 %s / t=%s / %.0fs" % (
                o["pct_kept"],
                "—" if o.get("excess_pp") is None else "%+.2f" % o["excess_pp"],
                "—" if o.get("win_rate") is None else "%.1f%%" % o["win_rate"],
                "—" if o.get("t") is None else "%.2f" % o["t"],
                r.get("elapsed_sec") or 0))

    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(a.outdir, "router_backtest_%s_T%d_%s.json" % (a.group, a.horizon, stamp))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"group": a.group, "horizon": a.horizon, "params": vars(a),
                   "rows": rows}, f, ensure_ascii=False, indent=1)
    table = fmt_table(rows, a.horizon, a.cost_bp)
    print("\n" + table)
    print("\n[out] %s（总耗时 %.0fs）" % (path, time.time() - t_all))
    with open(path.replace(".json", ".md"), "w", encoding="utf-8") as f:
        f.write(table + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
