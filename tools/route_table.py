# -*- coding: utf-8 -*-
"""路由表 v0：什么行情该开哪个工具 —— 2026-11-05

## 它回答的是哪个问题

之前所有回测都在回答「这个工具好不好」，但产品真正要回答的是
**「今天该开哪几个」**。一个工具全样本平均是 0，不代表它在所有行情里都是 0。
把分段结果按行情归类，才能看出「它在什么状态下为正」。

这正是之前缺的那一层：我做了无条件检验，然后把「平均 0」读成了「没用」。

## 口径

- 输入：`cond_engine_*.json` 里每个（工具 × 持有期）的 6 段结果
- 行情标签：用 `viewer/logs/ew_index.json` 算该段的全市场等权**年化**收益
      ↑ > +15%/年  → 上涨
      −15% ~ +15%  → 震荡
      < −15%/年    → 下跌
- 输出：每个工具在三种行情下的平均边缘、段数、同号比例
- **同号比例**是关键：3 段里 2 段同号才算有点稳定，1/3 那就是噪声

## 用法
  python tools/route_table.py
"""
from __future__ import annotations

import glob
import io
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "viewer" / "logs"
EW = OUT / "ew_index.json"


def load_ew() -> tuple[list[int], np.ndarray]:
    d = json.loads(EW.read_text(encoding="utf-8"))
    return [int(x) for x in d["dates"]], np.array(d["index"], dtype=np.float64)


def regime_of(ann_pct: float) -> str:
    if ann_pct > 15.0:
        return "上涨"
    if ann_pct < -15.0:
        return "下跌"
    return "震荡"


def main() -> None:
    ew_dates, ew_idx = load_ew()
    ewp = {d: i for i, d in enumerate(ew_dates)}

    files = sorted(glob.glob(str(OUT / "cond_engine_*_20261105.json")))
    files = [f for f in files if "_band" not in f and "_ctrl" not in f]
    if not files:
        print("没有 cond_engine 结果，先跑 tools/cond_engine.py")
        return
    print("读入：%s" % ", ".join(Path(f).name for f in files))

    # tool|H -> {regime: [(edge, ann, from, to), ...]}
    bucket: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for fn in files:
        d = json.loads(Path(fn).read_text(encoding="utf-8"))
        for key, r in (d.get("report") or {}).items():
            if not isinstance(r, dict) or not r.get("segments"):
                continue
            for s in r["segments"]:
                a = ewp.get(int(s["from"]))
                b = ewp.get(int(s["to"]))
                if a is None or b is None or b <= a or ew_idx[a] <= 0:
                    continue
                gross = ew_idx[b] / ew_idx[a] - 1.0
                yrs = (b - a) / 252.0
                if yrs <= 0:
                    continue
                ann = ((1.0 + gross) ** (1.0 / yrs) - 1.0) * 100.0
                bucket["%s|T+%d" % (r["tool"], r["hold"])][regime_of(ann)].append(
                    (s["mean"], round(ann, 1), s["from"], s["to"]))

    # 每个工具：取它 |t| 最大的那个持有期做主口径，另附其它持有期
    print("\n== 路由表 v0：每个（工具×持有期）在三种行情下的平均边缘 ==")
    print("   边缘单位 pp；「同号」= 该行情下各段与平均值同符号的比例；低于 2/3 视为不稳定\n")
    print("%-22s %-5s | %-22s | %-22s | %-22s"
          % ("工具", "持有", "上涨", "震荡", "下跌"))

    def fmt(items: list) -> str:
        if not items:
            return "—"
        v = np.array([x[0] for x in items])
        m = float(v.mean())
        same = int((np.sign(v) == np.sign(m)).sum())
        return "%+6.2f (n=%d 同号%d/%d)" % (m, len(v), same, len(v))

    rows = []
    for key in sorted(bucket):
        tool, hh = key.rsplit("|", 1)
        b = bucket[key]
        tot = sum(len(v) for v in b.values())
        if tot < 3:
            continue
        print("%-22s %-5s | %-22s | %-22s | %-22s"
              % (tool, hh, fmt(b.get("上涨", [])), fmt(b.get("震荡", [])), fmt(b.get("下跌", []))))
        for rg in ("上涨", "震荡", "下跌"):
            items = b.get(rg) or []
            if len(items) < 2:
                continue
            v = np.array([x[0] for x in items])
            m = float(v.mean())
            same = int((np.sign(v) == np.sign(m)).sum())
            rows.append({"tool": tool, "hold": hh, "regime": rg,
                         "mean_pp": round(m, 3), "n_seg": len(items),
                         "same_sign": same,
                         "stable": same >= max(2, int(np.ceil(len(items) * 2 / 3)))})

    path = OUT / "route_table_v0_20261105.json"
    path.write_text(json.dumps({"note": "edge=触发组−同池未触发组，按段行情归类",
                                "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n== 稳定可用的组合（同号 ≥2/3 且平均 |边缘| ≥0.3pp）==")
    ok = [r for r in rows if r["stable"] and abs(r["mean_pp"]) >= 0.3]
    if not ok:
        print("  无。所有工具在单一行情下的边缘都不稳定。")
    for r in sorted(ok, key=lambda x: -x["mean_pp"]):
        print("  %-8s %-5s %-6s 边缘 %+6.2fpp（%d 段，同号 %d）"
              % (r["tool"], r["hold"], r["regime"], r["mean_pp"], r["n_seg"], r["same_sign"]))
    print("\n写盘：%s" % path)


if __name__ == "__main__":
    main()
