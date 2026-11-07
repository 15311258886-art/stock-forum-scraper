"""A4 单票适配度：能不能「点开一只票，用每条判据在它自己的历史上测一遍」？

这个问题的直觉答案是「当然能」，但有一个绕不过去的算术问题：
一只票在 15 年里被某条判据触发多少次？如果只有 5~10 次，而单次 H 日收益的
横截面标准差是 25%~35%，那这只票「自己的历史超额」的抽样标准误就是 10pp 量级。
10pp 的误差区间里去分辨 1~2pp 的真差异，等于抛硬币。

本脚本做三件事：
  1. 数清楚：每条判据在单票上的触发次数分布（决定有没有资格谈单票）
  2. 算清楚：单票自身超额的「信噪比」 = 跨票离散度 / 抽样标准误
  3. 给结果：收缩估计后的单票分数 + 一句能不能用的判定
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(BASE, "viewer", "logs")
QFQ = os.path.join(BASE, "data", "kline_daily.db")


def sigma_h(H: int, n: int = 400) -> float:
    """估算「H 日持有收益」的横截面标准差（用一批票的全体样本池化）。"""
    con = sqlite3.connect(QFQ)
    codes = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT code FROM kline LIMIT ?", (n,))]
    vals = []
    for code in codes:
        df = pd.read_sql("SELECT d,c FROM kline WHERE code=? ORDER BY d", con,
                         params=(code,))
        c = df["c"].to_numpy(np.float64)
        if len(c) < H + 30:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            f = c[H:] / c[:-H] - 1.0
        f = f[np.isfinite(f)]
        if len(f):
            vals.append(f[:: max(1, len(f) // 50)])
    con.close()
    v = np.concatenate(vals) if vals else np.array([0.0])
    return float(np.std(v, ddof=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="20261105")
    ap.add_argument("--prior-k", type=float, default=20.0, help="收缩先验权重")
    ap.add_argument("--show", default="600519,000001,300750,002594,601318",
                    help="要举例的股票代码")
    args = ap.parse_args()

    npz = os.path.join(LOGS, "by_stock_%s.npz" % args.stamp)
    if not os.path.exists(npz):
        raise SystemExit("缺文件：%s（先跑 cond_matrix.py --by-stock）" % npz)
    z = np.load(npz, allow_pickle=False)
    codes = [str(x) for x in z["codes"]]
    keys = [k for k in z.files if k != "codes"]

    sig_cache: dict[int, float] = {}

    print("=" * 104)
    print("A4 单票适配度 ｜ %d 只票 ｜ %d 个工具" % (len(codes), len(keys)))
    print("=" * 104)
    print("\n【1】每条判据在单票上的触发次数分布（15 年历史）")
    print("%-24s %4s %4s %8s %8s %7s %7s" %
          ("工具", "进场", "持有", "中位次数", "均值次数", "n≥10占", "n≥30占"))

    summ = {}
    for k in keys:
        tool, e, H = k.split("|")
        e, H = int(e), int(H)
        a = z[k]
        ntr = a[1]
        pos = ntr > 0
        if not pos.any():
            continue
        med = float(np.median(ntr[pos]))
        mean = float(ntr.mean())
        r10 = float((ntr >= 10).mean()) * 100
        r30 = float((ntr >= 30).mean()) * 100
        summ[k] = dict(tool=tool, e=e, H=H, med=med, mean=mean, r10=r10, r30=r30)
        print("%-24s %4d %4d %8.0f %8.1f %6.1f%% %6.1f%%"
              % (tool, e, H, med, mean, r10, r30))

    print("\n【2】信噪比：单票自身超额的跨票离散度 ÷ 抽样标准误")
    print("    比值 <1 表示跨票的差别完全可以用抽样噪声解释，单票分数没有信息量")
    print("%-24s %4s %4s %8s %8s %8s %6s" %
          ("工具", "进场", "持有", "跨票sd", "中位se", "信噪比", "判定"))
    verdict = {}
    for k in keys:
        tool, e, H = k.split("|")
        e, H = int(e), int(H)
        a = z[k]
        st, nt, sn, nn = a[0], a[1], a[2], a[3]
        ok = (nt >= 5) & (nn >= 20)
        if ok.sum() < 30:
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            own = (st / nt - sn / nn) * 100.0
        own = own[ok]
        own = own[np.isfinite(own)]
        if len(own) < 30:
            continue
        if H not in sig_cache:
            sig_cache[H] = sigma_h(H) * 100.0
        sig = sig_cache[H]
        se = sig * np.sqrt(1.0 / nt[ok] + 1.0 / nn[ok])
        se = se[np.isfinite(se)]
        sd = float(np.std(own, ddof=1))
        mse = float(np.median(se))
        ratio = sd / mse if mse > 0 else float("nan")
        v = "可用" if ratio >= 2.0 else ("偏弱" if ratio >= 1.3 else "噪声")
        verdict[k] = dict(tool=tool, e=e, H=H, sd=sd, se=mse, ratio=ratio,
                          verdict=v, n_ok=int(ok.sum()))
        print("%-24s %4d %4d %8.2f %8.2f %8.2f %6s"
              % (tool, e, H, sd, mse, ratio, v))

    if not verdict:
        print("\n（没有任何工具同时满足 触发≥5 次 & 非触发≥20 天 的票数 ≥30，单票口径不成立）")

    print("\n【3】收缩后的单票分数（k=%.0f：自身样本占 n/(n+k)，其余按池子均值）" % args.prior_k)
    want = [c.strip() for c in args.show.split(",") if c.strip()]
    idx = {c: i for i, c in enumerate(codes)}
    body = [v for v in verdict.values() if v["verdict"] != "噪声"] or list(verdict.values())
    for tool in [v["tool"] for v in body][:3]:
        ks = [k for k in keys if k.split("|")[0] == tool]
        if not ks:
            continue
        k = ks[0]
        a = z[k]
        st, nt, sn, nn = a[0], a[1], a[2], a[3]
        gp = (st.sum() / max(nt.sum(), 1) - sn.sum() / max(nn.sum(), 1)) * 100.0
        print("\n  %s（池子均值 %+.2fpp，%s）" % (tool, gp, k))
        print("  %-8s %6s %10s %10s %8s" % ("代码", "触发", "自身超额", "收缩后", "偏离池"))
        rows = []
        for c in want:
            if c not in idx:
                continue
            i = idx[c]
            if nt[i] <= 0 or nn[i] <= 0:
                continue
            own = (st[i] / nt[i] - sn[i] / nn[i]) * 100.0
            shr = (nt[i] * own + args.prior_k * gp) / (nt[i] + args.prior_k)
            rows.append((c, int(nt[i]), own, shr, shr - gp))
        rows.sort(key=lambda x: -x[3])
        for c, n, own, shr, dev in rows:
            print("  %-8s %6d %+10.2f %+10.2f %+8.2f" % (c, n, own, shr, dev))

    out = os.path.join(LOGS, "stock_fitness_%s.json" % args.stamp)
    json.dump(dict(meta=dict(n_stocks=len(codes), prior_k=args.prior_k,
                             sigma_by_hold=sig_cache),
                   summary=summ, verdict=verdict),
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n写出 %s" % out)


if __name__ == "__main__":
    main()
