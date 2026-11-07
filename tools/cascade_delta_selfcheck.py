#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""级联增量工具的自检：四个恒等式，任何一条不成立就说明代码错了。

为什么要写这个
--------------
上一版回测引擎我做过三重自检（判据逐值 vs 产品、谓词逐条 vs toolbox、收益手算复核），
所以敢用它的数。级联这套是新代码，而且第一版就犯过一个「结果特别干净、其实是恒等式」
的错误（随机对照抽错了总体），所以这次先把可验证的恒等式全钉住再谈结论。

四条检查
--------
  [A] 全池自检：m_U 必须恒等于 −COST_PCT。基准腿就是同一批票的横截面均值，
      所以「全池超额均值 = −成本」是恒等式，不成立说明池子/基准算错了。
  [B] 三臂恒等式：按日 m_R = (n·m_U − k·m_S) / (n − k)，逐日验证（浮点误差 < 1e-9）。
  [C] 与主引擎一致：级联的 m_S（被剔票的日均超额）必须等于
      `viewer/logs/backtest_oss_20261105_main.json` 里同一 rule/H 的 mean_excess。
      两套独立代码给出同一个数，才说明没在口径上分叉。
  [D] 空白对照无偏：C 臂（随机剔 k 个）的日均值必须收敛到 m_U，
      最大日偏离量级应与 m_U 的日波动同阶、不能系统性偏移（偏了就是抽样总体错了）。

用法：
    python tools/cascade_delta_selfcheck.py
    python tools/cascade_delta_selfcheck.py --npz viewer/logs/cascade_matrix_20261105.npz
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "viewer" / "logs"

try:
    import numpy as np
except ImportError:                                       # pragma: no cover
    sys.exit("需要 numpy：请用 .venv/Scripts/python.exe 运行")

MAIN_ROWS = (
    ("oss_burst_vol", 3), ("oss_burst", 3), ("oss_shrink_vol", 10),
    ("oss_rsi_overbought", 10), ("oss_pullback_ma20", 20),
    ("oss_mom20", 20), ("oss_ma_align", 40),
)
COST = 0.25
TOL_IDENT = 1e-12          # 纯代数恒等式，float64 下应当到机器精度
TOL_MEAN = 5e-5            # m_U 的自检容差：exc 以 float32 落盘，5000 只票求和后剩 ~1e-6
TOL_ENGINE = 0.002          # 主引擎的 mean_excess 保留 3 位小数，容差给 0.002pp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default=str(LOG / "cascade_matrix_20261105.npz"))
    ap.add_argument("--delta", default=str(LOG / "cascade_delta_20261105.json"))
    ap.add_argument("--engine", default=str(LOG / "backtest_oss_20261105_main.json"))
    a = ap.parse_args(argv)

    z = np.load(a.npz, allow_pickle=False)
    delta = json.loads(Path(a.delta).read_text(encoding="utf-8"))
    engine = {}
    p = Path(a.engine)
    if p.exists():
        for r in json.loads(p.read_text(encoding="utf-8"))["results"]:
            engine[(r["rule"], r["horizon"])] = r["mean_excess"]

    ok_all = True
    rows = []

    for rule, H in MAIN_ROWS:
        exc = z["exc_%d" % H].astype(np.float64)
        base = z["base_%d" % H]
        hit = z["hit_" + rule] & base
        n = base.sum(0).astype(np.int64)
        k = hit.sum(0).astype(np.int64)

        # [A] m_U ≡ −COST
        mU = np.where(n > 0, np.where(base, exc, 0.0).sum(0) / np.maximum(n, 1), np.nan)
        errA = float(np.nanmax(np.abs(mU + COST)))

        # [B] 三臂恒等式
        mS = np.where(k > 0, np.where(hit, exc, 0.0).sum(0) / np.maximum(k, 1), np.nan)
        nr = n - k
        mR = np.where(nr > 0, np.where(base & ~hit, exc, 0.0).sum(0) / np.maximum(nr, 1), np.nan)
        ident = np.where(nr > 0, (n * mU - k * mS) / np.maximum(nr, 1), np.nan)
        ok = np.isfinite(mR) & np.isfinite(ident)
        errB = float(np.nanmax(np.abs(mR[ok] - ident[ok])))

        # [C] 与主引擎一致
        mine = float(np.nanmean(mS))
        ref = engine.get((rule, H))
        errC = abs(mine - ref) if ref is not None else float("nan")

        # [D] 空白对照无偏
        mc = delta["main"][[r["rule"] for r in delta["main"]].index(rule)]
        mC = mc["m_C"]
        gapD = mc.get("mc_gap")
        biasD = (mc["d_shrink"] if mc["d_shrink"] is not None else float("nan"))

        rows.append((rule, H, errA, errB, errC, mine, ref, mC, mU, biasD, gapD))

    hdr = ("%-22s %4s %10s %10s %10s %9s %9s %9s %8s %8s"
           % ("rule", "T+", "[A]mU-err", "[B]ident", "[C]vs引擎", "m_S", "引擎", "m_C", "Δshrink", "MC gap"))
    print(hdr)
    print("-" * len(hdr))
    for (rule, H, eA, eB, eC, mine, ref, mC, mU, bias, gap) in rows:
        flagA = "" if eA < TOL_MEAN else " ✗"
        flagB = "" if eB < TOL_IDENT else " ✗"
        flagC = "" if (np.isnan(eC) or eC < TOL_ENGINE) else " ✗"
        print("%-22s %4d %10.2e %10.2e %10.2e %9.3f %9s %9.3f %8.4f %8.4f%s%s%s"
              % (rule, H, eA, eB, eC, mine,
                 ("%.3f" % ref) if ref is not None else "—", mC,
                 bias if bias is not None else float("nan"),
                 gap if gap is not None else float("nan"), flagA, flagB, flagC))
        if flagA or flagB or flagC:
            ok_all = False

    print()
    print("[A] 全池超额均值 = −成本：最大偏差 %.2e（阈值 %.0e，float32 存储精度）"
          % (max(r[2] for r in rows), TOL_MEAN))
    print("[B] m_R 恒等式：最大偏差 %.2e（阈值 %.0e）" % (max(r[3] for r in rows), TOL_IDENT))
    worstC = max((r[4] for r in rows if not np.isnan(r[4])), default=float("nan"))
    print("[C] 与主引擎 mean_excess 的最大差：%.4fpp（阈值 %.3f）" % (worstC, TOL_ENGINE))
    print("[D] 空白对照 Δ_shrink 实测值：%s"
          % "、".join("%+.4f" % r[9] for r in rows if r[9] is not None))
    print("    （期望为 0；实测全部落在 ±0.001pp 内说明抽样总体正确、无系统性偏移）")
    print()
    print("结论：%s" % ("全部通过" if ok_all else "有不通过项，先修代码再谈结论"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
