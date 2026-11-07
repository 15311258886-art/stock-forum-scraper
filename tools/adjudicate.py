"""把矩阵结果转成「外部方法裁决表」：来源 / 它自己声称什么 / 实测什么 / 采纳与否。

这张表回答的是一个具体抱怨：GitHub 博主、公众号、抖音上讲「这票在什么历史时期
该用什么方法」，说法很多，但我们从没拿自己的数据逐条验过。现在逐条验，一条一行，
验不过的写清楚是怎么不过的。

裁决规则（写死，不许人工挑）：
  A 无证据     —— 所有格里没有一格同时满足 同号 + |block_t|≥2 + |diff|≥min_edge
  B 否决·反向  —— 最好的一格是负的。这个判据在 A 股是反指标，不是选股指标
  C 有条件采纳 —— 正向，但只在 1 个行情里过闸，或档位内出现反号（敏感）
  D 采纳       —— 正向、≥2 个行情过闸、档位内不敏感
另外标一列「声称 vs 实测是否一致」，供判断这个来源的可信度。
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
sys.path.insert(0, BASE)
LOGS = os.path.join(BASE, "viewer", "logs")

REAL = ("上涨", "震荡", "下跌")


def tier_of(H: int) -> str:
    return "短期" if H <= 5 else ("中期" if H <= 40 else "长期")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default="20261105")
    ap.add_argument("--in", dest="inp", default="")
    ap.add_argument("--min-edge", type=float, default=0.25)
    ap.add_argument("--min-block-t", type=float, default=2.0)
    args = ap.parse_args()

    inp = args.inp or os.path.join(LOGS, "cond_matrix_%s.json" % args.stamp)
    d = json.load(open(inp, encoding="utf-8"))
    cells = d["cells"]

    meta = {}
    try:
        from viewer.playbook import PLAYBOOK
        for k, v in PLAYBOOK.items():
            meta[k] = dict(name=v.get("name", k), src=v.get("src", ""),
                           claimed=v.get("claimed", ""), note=v.get("note", ""))
    except Exception as e:  # 判据库不可用时退化成只有实测
        print("（判据库不可用：%s）" % e)

    by_tool = defaultdict(list)
    for c in cells:
        by_tool[c["tool"]].append(c)

    rows = []
    for tool, cs in by_tool.items():
        passed = [c for c in cs
                  if c.get("consistent")
                  and abs(c.get("block_t") or 0) >= args.min_block_t
                  and abs(c.get("diff_pp") or 0) >= args.min_edge]
        m = meta.get(tool, {})
        rec = dict(tool=tool, name=m.get("name", ""), src=m.get("src", ""),
                   claimed=m.get("claimed", ""), note=m.get("note", ""),
                   n_cells=len(cs), n_pass=len(passed))
        if not passed:
            rec.update(verdict="A 无证据", best=None, regimes=[])
            rows.append(rec)
            continue
        best = max(passed, key=lambda c: abs(c["diff_pp"]))
        sign = 1 if best["diff_pp"] > 0 else -1
        reg = {}
        for rg in REAL:
            sub = [c for c in passed if c["regime"] == rg]
            if sub:
                top = max(sub, key=lambda c: abs(c["diff_pp"]))
                reg[rg] = round(top["diff_pp"], 3)
        same_regimes = [r for r, v in reg.items() if (v > 0) == (sign > 0)]
        band = [c for c in passed if tier_of(c["hold"]) == tier_of(best["hold"])]
        sensitive = len({1 if c["diff_pp"] > 0 else -1 for c in band}) > 1

        if sign < 0:
            verdict = "B 否决·反向"
        elif len(reg) >= 2 and len(same_regimes) >= 2 and not sensitive:
            verdict = "D 采纳"
        else:
            verdict = "C 有条件采纳"
        # 触发票数太少（日均 <30 只）时，日均序列的噪声压不住，就算过闸也只能算「待观察」
        thin = (best.get("avg_trig") or 0) < 30
        if thin and verdict.startswith("D"):
            verdict = "C 有条件采纳"

        rec.update(verdict=verdict, best=dict(
            regime=best["regime"], entry=best["entry"], hold=best["hold"],
            tier=tier_of(best["hold"]), diff_pp=best["diff_pp"],
            block_t=best["block_t"], hit=best.get("hit_pct"),
            n_days=best.get("n_days"), avg_trig=best.get("avg_trig")),
            regimes=reg, sensitive=sensitive)
        rows.append(rec)

    ordv = {"D 采纳": 0, "C 有条件采纳": 1, "B 否决·反向": 2, "A 无证据": 3}
    rows.sort(key=lambda r: (ordv[r["verdict"]],
                             -(abs(r["best"]["diff_pp"]) if r["best"] else 0)))

    print("=" * 118)
    print("外部方法裁决表 ｜ %d 个工具 / %d 格 ｜ 闸：同号 + |block_t|≥%.1f + |diff|≥%.2fpp"
          % (len(rows), len(cells), args.min_block_t, args.min_edge))
    print("=" * 118)
    print("%-24s %-10s %-6s %-4s %4s %8s %7s %6s %-6s %s"
          % ("工具", "裁决", "代表行情", "进场", "持有", "超额pp", "block_t", "胜率",
             "三行情", "来源/声称"))
    for r in rows:
        if not r["best"]:
            print("%-24s %-10s %s" % (r["tool"], r["verdict"], "—"))
            continue
        b = r["best"]
        rg = "/".join("%s%+.1f" % (k[0], v) for k, v in
                      sorted(r["regimes"].items(), key=lambda x: REAL.index(x[0])))
        src = (r["src"][:14] + ":" + r["claimed"][:14]) if r["src"] else "—"
        print("%-24s %-10s %-6s %-4s %4d %+8.3f %7.2f %6s %-6s %s"
              % (r["tool"][:24], r["verdict"], b["regime"], "e%d" % b["entry"],
                 b["hold"], b["diff_pp"], b["block_t"],
                 "-" if b["hit"] is None else "%.1f" % b["hit"], rg, src))

    cnt = defaultdict(int)
    for r in rows:
        cnt[r["verdict"]] += 1
    print("\n裁决分布：" + " ｜ ".join("%s %d" % (k, cnt[k]) for k in
                                   ("D 采纳", "C 有条件采纳", "B 否决·反向", "A 无证据")))

    outp = os.path.join(LOGS, "adjudicate_%s.json" % args.stamp)
    json.dump(dict(meta=dict(src=os.path.basename(inp), min_edge=args.min_edge,
                             min_block_t=args.min_block_t), rows=rows),
              open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    md = os.path.join(BASE, "docs", "外部方法裁决表_自动生成.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("# 外部方法裁决表（自动生成）\n\n")
        f.write("输入：`%s`，%d 个工具 / %d 格。闸：时间对半分同号 + |block_t|≥%.1f + |超额|≥%.2fpp。\n\n"
                % (os.path.basename(inp), len(rows), len(cells),
                   args.min_block_t, args.min_edge))
        f.write("| 工具 | 它的名字 | 来源 | 它声称什么 | 裁决 | 代表格 | 超额pp | block_t | 胜率 | 三行情(上涨/震荡/下跌) |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            b = r["best"]
            cell = "—" if not b else "%s e%d T+%d" % (b["regime"], b["entry"], b["hold"])
            rg = "—" if not b else " / ".join("%+.2f" % r["regimes"].get(x, float("nan"))
                                              if x in r["regimes"] else "·" for x in REAL)
            f.write("| `%s` | %s | %s | %s | **%s** | %s | %s | %s | %s | %s |\n"
                    % (r["tool"], r["name"] or "—", r["src"] or "—", r["claimed"] or "—",
                       r["verdict"], cell,
                       "—" if not b else "%+.3f" % b["diff_pp"],
                       "—" if not b else "%.2f" % b["block_t"],
                       "—" if not b or b["hit"] is None else "%.1f%%" % b["hit"], rg))
    print("写出 %s" % md)
    print("写出 %s" % outp)


if __name__ == "__main__":
    main()
