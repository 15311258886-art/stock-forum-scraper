"""A1 结算：把「结构化买点」的配对差，减掉随机信号上的机械效应。

为什么必须减：`pull_2pct`（回踩 2% 买）在**随机信号**上就有 +3.2pp 的配对差——
不是你选得准，是你买得便宜。任何工具的配对差只有超过这条基准线，多出来的
那部分才算「这个工具和这个买点之间真的有交互」。

本脚本输出三列：
  原始配对差 = 该买点收益 − 触发日收盘买收益（同一批信号）
  机械基准   = 随机 5% 信号在同一买点规则上的配对差
  差中差     = 原始 − 机械（>0 才算工具自己的本事）

以及两道过滤：block_t ≥ 2、时间对半分同号。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(BASE, "viewer", "logs")

RULE_CN = {
    "e1": "次日买",
    "e3": "第3日买",
    "pull_ma5": "回踩MA5不破",
    "pull_2pct": "回踩-2%",
    "brk_high": "破前高确认",
    "shrink_ma5": "缩量企稳MA5",
}


def load(stamp: str) -> dict:
    p = os.path.join(LOGS, "cond_entry_%s.json" % stamp)
    if not os.path.exists(p):
        raise SystemExit("缺文件：%s" % p)
    return json.load(open(p, encoding="utf-8"))


def approx_t(did: float, m: float, t: float, bm: float, bt: float) -> float:
    """差中差的近似 t：用两边各自的 NW t 反推标准误再合并。

    两边不是独立样本（同一批日期、重叠的股票），所以这个 t 偏保守也偏乐观
    都可能。它只用来筛，不用来下结论——下结论看 block_t 和对半分。
    """
    if not (t and bt) or not (abs(t) > 1e-9 and abs(bt) > 1e-9):
        return float("nan")
    se1 = abs(m / t)
    se2 = abs(bm / bt)
    se = math.sqrt(se1 * se1 + se2 * se2)
    return did / se if se > 0 else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="20261105")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    d = load(args.stamp)
    rows = d["rows"]
    base = {(r["rule"], r["hold"]): r for r in rows if r["tool"] == "random5"}
    tools = sorted({r["tool"] for r in rows if r["tool"] != "random5"})

    print("=" * 100)
    print("A1 差中差：结构化买点 vs 随机基准")
    print("=" * 100)
    print("\n【机械基准】随机 5% 信号在各买点上的配对差（这就是「买得便宜」本身的值）")
    print("%-12s %6s %10s %8s %8s %8s  %s"
          % ("买点", "持有", "配对差pp", "NW_t", "前半", "后半", "同号"))
    for rule in ("e1", "e3", "pull_ma5", "pull_2pct", "brk_high", "shrink_ma5"):
        for H in (3, 5, 10, 20, 60, 120):
            b = base.get((rule, H))
            if not b:
                continue
            print("%-12s %6d %10.3f %8.2f %8s %8s  %s"
                  % (RULE_CN.get(rule, rule), H, b["daily_mean_pp"],
                     b["nw_t"],
                     "-" if b.get("half1_pp") is None else "%.2f" % b["half1_pp"],
                     "-" if b.get("half2_pp") is None else "%.2f" % b["half2_pp"],
                     "是" if b.get("consistent") else "否"))

    out = []
    for r in rows:
        if r["tool"] == "random5":
            continue
        b = base.get((r["rule"], r["hold"]))
        if not b:
            continue
        did = r["daily_mean_pp"] - b["daily_mean_pp"]
        out.append(dict(
            tool=r["tool"], rule=r["rule"], rule_cn=RULE_CN.get(r["rule"], r["rule"]),
            hold=r["hold"], raw_pp=r["daily_mean_pp"], base_pp=b["daily_mean_pp"],
            did_pp=round(did, 3),
            t_did=round(approx_t(did, r["daily_mean_pp"], r["nw_t"],
                                 b["daily_mean_pp"], b["nw_t"]), 2),
            signals=r["signals"], block_t=r["block_t"],
            half1=r.get("half1_pp"), half2=r.get("half2_pp"),
            consistent=bool(r.get("consistent")) and bool(b.get("consistent")),
        ))

    ok = [o for o in out
          if o["consistent"] and abs(o["block_t"]) >= 2.0
          and math.isfinite(o["t_did"])]
    ok.sort(key=lambda x: -x["did_pp"])

    print("\n" + "=" * 100)
    print("【差中差排行】同时满足：时间对半分同号 + block_t≥2（共 %d 格，全部 %d 格）"
          % (len(ok), len(out)))
    print("=" * 100)
    print("%-24s %-10s %5s %9s %9s %9s %7s %7s %5s"
          % ("工具", "买点", "持有", "原始pp", "机械pp", "差中差", "t近似", "block_t", "n"))
    for o in ok[:40]:
        print("%-24s %-10s %5d %9.3f %9.3f %9.3f %7.2f %7.2f %5d"
              % (o["tool"], o["rule_cn"], o["hold"], o["raw_pp"], o["base_pp"],
                 o["did_pp"], o["t_did"], o["block_t"], o["signals"]))

    neg = [o for o in ok if o["did_pp"] < 0]
    if neg:
        print("\n【差中差为负】这个买点对这些工具是帮倒忙（共 %d 格，列最差 15）" % len(neg))
        print("%-24s %-10s %5s %9s %9s %9s"
              % ("工具", "买点", "持有", "原始pp", "机械pp", "差中差"))
        for o in sorted(neg, key=lambda x: x["did_pp"])[:15]:
            print("%-24s %-10s %5d %9.3f %9.3f %9.3f"
                  % (o["tool"], o["rule_cn"], o["hold"], o["raw_pp"],
                     o["base_pp"], o["did_pp"]))

    # 每个工具最好的一个买点：回答「这工具该在什么时点进」
    print("\n" + "=" * 100)
    print("【每工具最优进场时点】只取通过两道过滤的格子里差中差最大的一个")
    print("=" * 100)
    print("%-24s %-10s %5s %9s %9s %5s" % ("工具", "买点", "持有", "差中差", "原始pp", "改善%"))
    best = {}
    for o in ok:
        k = o["tool"]
        if k not in best or o["did_pp"] > best[k]["did_pp"]:
            best[k] = o
    for k in sorted(best, key=lambda x: -best[x]["did_pp"]):
        o = best[k]
        imp = (o["raw_pp"] / o["base_pp"] * 100) if abs(o["base_pp"]) > 1e-6 else float("nan")
        print("%-24s %-10s %5d %9.3f %9.3f %5s"
              % (k, o["rule_cn"], o["hold"], o["did_pp"], o["raw_pp"],
                 "-" if not math.isfinite(imp) else "%+.0f" % imp))

    outp = args.out or os.path.join(LOGS, "entry_did_%s.json" % args.stamp)
    json.dump(dict(meta=d.get("meta"), base={"%s|%d" % k: v for k, v in base.items()},
                   rows=out, passed=ok),
              open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n写出 %s（%d 格）" % (outp, len(out)))


if __name__ == "__main__":
    main()
