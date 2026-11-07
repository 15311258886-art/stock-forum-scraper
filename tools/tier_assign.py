"""A3 自动定档：把 1620/3024 格矩阵压成「每个工具在什么行情、什么持有期、什么方向用」。

定档规则全部写死在这里，不允许人工挑格子——否则等于我在上千格里挑最大值，
必然挑出一个漂亮数字，那叫拟合，不叫结论。

规则（每条都可被推翻，改这里就行）：
  R1 过闸：consistent（时间对半分同号）+ |block_t| ≥ 2 + |diff| ≥ min_edge
  R2 方向：过闸格子里 |diff| 最大那格（代表格）的符号 → 正向=选股/加权，反向=剔除/降权
  R3 峰值：代表格的持有期，只作参考，**不当档位**
  R4 档位：取「与代表格同向、同行情」的过闸格子里**最短的持有期**（起效点）分档
           ≤5 短期 / 6~40 中期 / ≥60 长期
           为什么不用峰值定档：|超额pp| 天然随持有期放大（复利+离散度），
           按绝对值择优会把所有工具都推到长期，这是机械效应不是发现。
  R5 全行情反向：三个真实行情（上涨/震荡/下跌）里 ≥2 个都过闸且反向 → 标「全行情反向」，
           产品动作是直接下线，不是「换个时候用」
  R6 行情敏感：同一档位内出现过正负反号 → 标「行情敏感」
  R7 无证据：一格都没过 → 不进产品
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(BASE, "viewer", "logs")

REAL = ("上涨", "震荡", "下跌")


def tier_of(H: int) -> str:
    if H <= 5:
        return "短期"
    if H <= 40:
        return "中期"
    return "长期"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="")
    ap.add_argument("--min-edge", type=float, default=0.25)
    ap.add_argument("--min-block-t", type=float, default=2.0)
    ap.add_argument("--stamp", default="20261105")
    args = ap.parse_args()

    inp = args.inp or os.path.join(LOGS, "cond_matrix_%s.json" % args.stamp)
    d = json.load(open(inp, encoding="utf-8"))
    cells = d["cells"]

    by_tool = defaultdict(list)
    for c in cells:
        by_tool[c["tool"]].append(c)

    out = []
    for tool, cs in by_tool.items():
        passed = [c for c in cs
                  if c.get("consistent")
                  and abs(c.get("block_t") or 0) >= args.min_block_t
                  and abs(c.get("diff_pp") or 0) >= args.min_edge]
        rec = dict(tool=tool, grp=cs[0].get("grp"), n_cells=len(cs),
                   n_pass=len(passed))
        if not passed:
            rec.update(tier="无证据", direction="—", regime="—", peak_hold=None,
                       onset_hold=None, entry=None, diff_pp=None, block_t=None,
                       hits=None, all_regime_rev=False, sensitive=False,
                       support=[], regimes_pass=[])
            out.append(rec)
            continue

        peak = max(passed, key=lambda c: abs(c["diff_pp"]))
        sign = 1 if peak["diff_pp"] > 0 else -1

        same = [c for c in passed
                if c["regime"] == peak["regime"] and (1 if c["diff_pp"] > 0 else -1) == sign]
        onset = min(c["hold"] for c in same)

        # R5：三个真实行情里各取「最短且同向」的格子，统计反向覆盖面
        reg_sign = {}
        for rg in REAL:
            sub = [c for c in passed if c["regime"] == rg]
            if sub:
                reg_sign[rg] = (1 if max(sub, key=lambda c: abs(c["diff_pp"]))["diff_pp"] > 0 else -1)
        n_rev = sum(1 for v in reg_sign.values() if v < 0)

        # R6：同档位内是否出现反号
        band = [c for c in passed if tier_of(c["hold"]) == tier_of(onset)]
        sensitive = len({1 if c["diff_pp"] > 0 else -1 for c in band
                         if abs(c["diff_pp"]) >= args.min_edge}) > 1

        rec.update(tier=tier_of(onset), onset_hold=onset,
                   peak_hold=peak["hold"], entry=peak["entry"],
                   regime=peak["regime"], diff_pp=peak["diff_pp"],
                   block_t=peak["block_t"], hits=peak.get("hit_pct"),
                   direction="正向" if sign > 0 else "反向",
                   all_regime_rev=(n_rev >= 2),
                   sensitive=sensitive,
                   regimes_pass=sorted(reg_sign),
                   support=sorted({c["hold"] for c in same}))
        out.append(rec)

    order = {"短期": 0, "中期": 1, "长期": 2, "无证据": 3}
    out.sort(key=lambda r: (order.get(r["tier"], 9),
                            not r["all_regime_rev"] if r["direction"] == "反向" else True,
                            -(abs(r["diff_pp"]) if r["diff_pp"] is not None else 0)))

    print("=" * 112)
    print("A3 自动定档 ｜ %s ｜ %d 格 → %d 工具 ｜ 闸：同号 + |block_t|≥%.1f + |diff|≥%.2fpp"
          % (os.path.basename(inp), len(cells), len(out),
             args.min_block_t, args.min_edge))
    print("=" * 112)
    print("%-24s %-4s %-4s %-6s %-4s %4s %4s %8s %7s %6s %-8s %s"
          % ("工具", "档位", "方向", "代表行情", "进场", "起效", "峰值",
             "峰值pp", "block_t", "胜率", "全行情反", "过闸/行情数"))
    for r in out:
        if r["tier"] == "无证据":
            print("%-24s %-4s %s" % (r["tool"], r["tier"], "%d 格全灭" % r["n_cells"]))
            continue
        print("%-24s %-4s %-4s %-6s %-4s %4d %4d %+8.3f %7.2f %6s %-8s %d/%d%s"
              % (r["tool"], r["tier"], r["direction"], r["regime"],
                 "e%d" % r["entry"], r["onset_hold"], r["peak_hold"],
                 r["diff_pp"], r["block_t"],
                 "-" if r["hits"] is None else "%.1f" % r["hits"],
                 "是" if r["all_regime_rev"] else "否",
                 r["n_pass"], len(r["regimes_pass"]),
                 "  ⚠敏感" if r["sensitive"] else ""))

    n_ok = sum(1 for r in out if r["tier"] != "无证据")
    n_fwd = sum(1 for r in out if r["direction"] == "正向")
    n_ar = sum(1 for r in out if r["all_regime_rev"])
    print("\n合计：%d 工具过闸 ｜ 正向 %d ｜ 反向 %d（其中全行情反向 %d）｜ 无证据 %d"
          % (n_ok, n_fwd, n_ok - n_fwd, n_ar, len(out) - n_ok))

    for t in ("短期", "中期", "长期"):
        sel = [r for r in out if r["tier"] == t and r["direction"] == "正向"]
        print("\n【%s·可正向用】%d 个：%s"
              % (t, len(sel),
                 "、".join("%s@%s起效T+%d" % (r["tool"], r["regime"], r["onset_hold"])
                          for r in sel) or "无"))

    outp = os.path.join(LOGS, "tier_assign_%s.json" % args.stamp)
    json.dump(dict(meta=dict(src=os.path.basename(inp), min_edge=args.min_edge,
                             min_block_t=args.min_block_t,
                             rules="R1过闸/R2方向/R3峰值/R4档位=起效点/R5全行情反向/R6敏感/R7无证据"),
                   tools=out), open(outp, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n写出 %s" % outp)


if __name__ == "__main__":
    main()
