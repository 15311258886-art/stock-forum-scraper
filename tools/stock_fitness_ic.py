"""A4 的关键检验：一只票「前半段的自身历史」，能不能预测它的「后半段」？

单票适配度要成立，必须满足：同一只票早年的触发超额，和它晚年的触发超额是同号的、
且排序稳定。否则「点开这只票看它历史」得到的只是一个抽样噪声，看起来像个性，其实不是。

不跑这个检验就下结论，等于把「跨票差异大」当成「单票有信号」——前者可能只是
不同票的长期趋势不同（688585 那种一路涨的票，「近高点」触发 509 次、自身超额 +262pp，
那不是判据灵，那是它涨）。

输出：
  IC        = 前半段单票超额 与 后半段单票超额 的 Spearman 秩相关
  五分位价差 = 按前半段排序，最好 20% 与最差 20% 在后半段的实际超额之差
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(BASE, "viewer", "logs")


def edge(seg_sum_t, n_t, seg_sum_n, n_n, min_t=5, min_n=20):
    ok = (n_t >= min_t) & (n_n >= min_n)
    with np.errstate(divide="ignore", invalid="ignore"):
        e = (seg_sum_t / np.maximum(n_t, 1) - seg_sum_n / np.maximum(n_n, 1)) * 100.0
    return e, ok, n_t, n_n


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 30:
        return float("nan")
    ra = np.argsort(np.argsort(a)).astype(np.float64)
    rb = np.argsort(np.argsort(b)).astype(np.float64)
    ra -= ra.mean()
    rb -= rb.mean()
    d = np.sqrt((ra * ra).sum() * (rb * rb).sum())
    return float((ra * rb).sum() / d) if d > 0 else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="20261105")
    ap.add_argument("--min-t", type=int, default=5)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    npz = os.path.join(LOGS, "by_stock_%s.npz" % args.stamp)
    z = np.load(npz, allow_pickle=False)
    keys = [k for k in z.files if k != "codes"]

    print("=" * 108)
    print("A4 关键检验：前半段单票历史 → 后半段实际表现")
    print("=" * 108)
    print("%-24s %5s %7s %8s %10s %10s %8s %s"
          % ("工具", "样本票", "IC", "五分位差", "前半最好20%", "前半最差20%", "单调性", "判定"))

    rows = []
    for k in keys:
        tool, e, H = k.split("|")
        a = z[k]
        if a.shape[0] < 8:
            continue
        e1, ok1, n1, m1 = edge(a[0], a[1], a[2], a[3], args.min_t)
        e2, ok2, n2, m2 = edge(a[4], a[5], a[6], a[7], args.min_t)
        ok = ok1 & ok2 & np.isfinite(e1) & np.isfinite(e2)
        if ok.sum() < 50:
            continue
        x, y = e1[ok], e2[ok]
        ic = spearman(x, y)

        q = np.quantile(x, [0.2, 0.4, 0.6, 0.8])
        bins = np.digitize(x, q)
        means = [float(y[bins == i].mean()) for i in range(5)]
        spread = means[4] - means[0]
        mono = sum(1 for i in range(4) if means[i + 1] >= means[i])

        if not np.isfinite(ic):
            v = "样本不足"
        elif ic >= 0.15 and spread > 0:
            v = "可用"
        elif ic >= 0.05 and spread > 0:
            v = "偏弱"
        else:
            v = "不可用"

        rows.append(dict(tool=tool, entry=int(e), hold=int(H), n=int(ok.sum()),
                         ic=round(ic, 3) if np.isfinite(ic) else None,
                         spread=round(spread, 2), means=[round(m, 2) for m in means],
                         mono=mono, verdict=v))
        print("%-24s %5d %7.3f %+8.2f %+10.2f %+10.2f %6d/4 %s"
              % (tool, int(ok.sum()), ic, spread, means[4], means[0], mono, v))

    if rows:
        ok = [r for r in rows if r["verdict"] == "可用"]
        print("\n可用 %d / 参与检验 %d 个" % (len(ok), len(rows)))
        if ok:
            print("可用的：" + "、".join(r["tool"] for r in ok))
        print("\n读法：")
        print("  IC ≈ 0      → 前半段的单票历史对后半段没有排序能力，单票分数就是噪声")
        print("  五分位差 >0 → 按前半段挑「这只票历史上更灵」确实有点用，才轮得到做产品")
        print("  单调性 4/4  → 五个分位从差到好一路抬升，比单看 IC 更直观")

    outp = args.out or os.path.join(LOGS, "stock_fitness_ic_%s.json" % args.stamp)
    json.dump(dict(meta=dict(min_t=args.min_t), rows=rows),
              open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n写出 %s" % outp)


if __name__ == "__main__":
    main()
