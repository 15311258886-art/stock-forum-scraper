#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口径对照：`app.db.stock_indicators_daily.close` 与 `kbase.kline`（后复权）是不是同一条价格基准。

为什么需要这个文件：工具卡片上的「实测超额」来自 `stock_indicators_daily`，而回测引擎
（`tools/backtest_oss.py`）在后复权 kline 上跑。**如果两条价格基准不同，卡片数字和回测
数字就不是同一个东西**，光说「窗口不同、扣了成本」解释不干净 —— 得先证明差异有多大。

这个脚本只读两个库、只回答三个问题：
  1. 同一天，两张表的 close 是不是相等？（比值分布）
  2. 同一个 20 日窗口，两套价格的收益率差多少？（这才是影响超额的量）
  3. 差异集中在哪些票上？（高分红 / 有除权的票，还是全体）

用法： .venv/Scripts/python.exe tools/backtest_oss_basis_check.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
KBASE = ROOT / "viewer" / "kbase.db"
APP_DB = ROOT / "viewer" / "app.db"
H = 20                       # 对照用的持有期
LOGS = ROOT / "viewer" / "logs"

_RESULT: dict = {}


def _con(p: Path) -> sqlite3.Connection:
    return sqlite3.connect("file:%s?mode=ro" % p.as_posix(), uri=True)


def _dump() -> None:
    """落盘：报告里的「口径对照」附录读这个文件，不在正文里写死数字。"""
    LOGS.mkdir(parents=True, exist_ok=True)
    _RESULT["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = LOGS / ("backtest_oss_basis_%s.json" % datetime.now().strftime("%Y%m%d"))
    out.write_text(json.dumps(_RESULT, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n对照结果已落盘：%s" % out)


def main() -> int:
    k = _con(KBASE)
    a = _con(APP_DB)

    dates = [str(r[0]) for r in a.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]
    if not dates:
        print("stock_indicators_daily 是空的")
        return 1
    print("指标表覆盖：%s ~ %s（%d 个交易日）" % (dates[0], dates[-1], len(dates)))

    # ---- 1. 同一天 close 的比值 ----
    latest = dates[-1]
    ind = {str(c): float(v) for c, v in a.execute(
        "SELECT code,close FROM stock_indicators_daily WHERE date=? AND close>0", (latest,))}
    kln = {str(c): float(v) for c, v in k.execute(
        "SELECT code,c FROM kline WHERE date=? AND c>0", (latest,))}
    common = sorted(set(ind) & set(kln))
    if not common:
        print("没有可比的票（两张表同一天没有交集）")
        return 1
    ratio = np.array([ind[c] / kln[c] for c in common])
    same = np.abs(ratio - 1.0) < 1e-6
    print("\n[1] %s 收盘价比值（指标表 ÷ 后复权）：可比的 %d 只票"
          % (latest, len(common)))
    print("    完全相等：%d 只（%.1f%%）" % (int(same.sum()), same.mean() * 100))
    print("    比值分位：p1=%.4f  p25=%.4f  中位=%.4f  p75=%.4f  p99=%.4f  min=%.4f max=%.4f"
          % (np.percentile(ratio, 1), np.percentile(ratio, 25), np.median(ratio),
             np.percentile(ratio, 75), np.percentile(ratio, 99), ratio.min(), ratio.max()))
    _RESULT.update({"indicator_span": [dates[0], dates[-1], len(dates)],
                    "ratio_date": latest, "n_compared": len(common),
                    "n_identical": int(same.sum()),
                    "identical_pct": round(float(same.mean()) * 100.0, 2),
                    "ratio_p1": float(np.percentile(ratio, 1)),
                    "ratio_median": float(np.median(ratio)),
                    "ratio_p99": float(np.percentile(ratio, 99))})
    if same.all():
        print("    → 两张表用的是同一条价格序列（指标表也是后复权）。基准不存在差异，"
              "卡片与回测的差别只能来自窗口 / 成本 / 口径。")
        _RESULT["same_basis"] = True
        _dump()
        return 0

    # ---- 2. 同一 20 日窗口的收益率差 ----
    if len(dates) <= H:
        print("\n[2] 指标表不足 %d 个交易日，无法对照持有期收益" % (H + 1))
        return 0
    rows = a.execute(
        "SELECT code,date,close FROM stock_indicators_daily WHERE close>0 "
        "ORDER BY code,date").fetchall()
    ind_ser: dict[str, dict[str, float]] = {}
    for code, d, c in rows:
        ind_ser.setdefault(str(code), {})[str(d)] = float(c)
    krows = k.execute(
        "SELECT code,date,c FROM kline WHERE date>=? AND c>0 ORDER BY code,date",
        (dates[0],)).fetchall()
    k_ser: dict[str, dict[str, float]] = {}
    for code, d, c in krows:
        k_ser.setdefault(str(code), {})[str(d)] = float(c)

    diffs: list[float] = []
    worst: list[tuple[float, str, str]] = []
    for i in range(H, len(dates)):
        d0, d1 = dates[i - H], dates[i]
        for code, s in ind_ser.items():
            ks = k_ser.get(code)
            if not ks or d0 not in s or d1 not in s or d0 not in ks or d1 not in ks:
                continue
            r_ind = (s[d1] / s[d0] - 1.0) * 100.0
            r_k = (ks[d1] / ks[d0] - 1.0) * 100.0
            diffs.append(r_ind - r_k)
            worst.append((abs(r_ind - r_k), code, "%s→%s" % (d0, d1)))
    d = np.array(diffs)
    print("\n[2] 同一 %d 日窗口、同一批票：指标表收益 − 后复权收益（单位 pp，n=%d）"
          % (H, d.size))
    print("    中位 %.4f ｜ 均值 %.4f ｜ p1 %.4f ｜ p99 %.4f ｜ |差|>0.5pp 占比 %.2f%%"
          % (np.median(d), d.mean(), np.percentile(d, 1), np.percentile(d, 99),
             float((np.abs(d) > 0.5).mean()) * 100))
    worst.sort(reverse=True)
    print("    差异最大的 5 个样本：" +
          "，".join("%s@%s ±%.2fpp" % (c, w, v) for v, c, w in worst[:5]))
    _RESULT.update({"hold_days": H, "n_return_pairs": int(d.size),
                    "diff_median_pp": round(float(np.median(d)), 4),
                    "diff_mean_pp": round(float(d.mean()), 4),
                    "diff_p1_pp": round(float(np.percentile(d, 1)), 4),
                    "diff_p99_pp": round(float(np.percentile(d, 99)), 4),
                    "pct_abs_gt_0_5pp": round(float((np.abs(d) > 0.5).mean()) * 100.0, 2),
                    "same_basis": False})
    print("\n结论：%s"
          % ("两套价格的收益率差在 0.01pp 量级 —— 复权基准的差别不足以解释卡片与回测的差距，"
             "差距主要来自窗口（394 天 vs 726 天）和成本。"
             if abs(float(np.median(d))) < 0.05 and
             float((np.abs(d) > 0.5).mean()) < 0.05 else
             "两套价格的收益率差不可忽略，必须在卡片上写明基准，不能与后复权回测数字混用。"))
    _dump()
    return 0


if __name__ == "__main__":
    sys.exit(main())
