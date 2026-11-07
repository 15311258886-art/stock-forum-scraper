# -*- coding: utf-8 -*-
"""T08 到期打分：从账本读「历史决策 → 到期收益」，按 工具 × 持有期 出成绩单。

为什么需要它
    `tools/router_backtest.py --log-decisions` 只是把样本写进 `tool_outcome`。
    样本躺着不会自己变成判断 —— 必须有一步把它**按决策日聚合**成胜率/超额/t 值，
    再拿这个数去回答「这个工具下次还该不该开」。

口径（与 D-20261106-backtest 一致，不许另立一套）
    · 超额 = 毛收益 − 同时点同池基准 − 成本（成本在回测落库时已扣，这里不再扣）
    · t 值 **按决策日聚合**：先算每天的入选票平均超额，再对日序列求 t。
      不按票堆样本 —— 同一天入选的 50 只票不是 50 个独立观测，
      那样算出来的 t 会被放大几倍，任何工具都能"显著"。
    · 只统计 `status='BT'` 的回测样本（线上真实决策另算，两者绝不混统计）

用法
    python tools/score_ledger.py
    python tools/score_ledger.py --min-days 20
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:    # 成本口径单一事实源（P4 统一 15bp）；缺依赖时退回旧文本不阻断
    from viewer.kernel import params as _P
except Exception:  # noqa: BLE001
    _P = None

APP_DB = os.path.join(ROOT, "viewer", "app.db")


def tstat(xs: list):
    """按日聚合后的 t 值。"""
    if len(xs) < 3:
        return None
    m = statistics.fmean(xs)
    sd = statistics.pstdev(xs)
    if sd == 0:
        return None
    return m / (sd / math.sqrt(len(xs)))


def score(con: sqlite3.Connection, status: str = "BT") -> list:
    """→ [{tool, horizon, days, n, excess_pp, win_rate, t}]，按 |t| 降序。"""
    rows = con.execute(
        "SELECT d.chain_json, d.horizon_days, d.as_of, o.net_excess_return, o.hit"
        " FROM tool_outcome o JOIN tool_decision_log d ON d.decision_id = o.decision_id"
        " WHERE d.status = ? AND o.status = 'scored'", (status,)).fetchall()

    # 先按 (工具, 持有期, 决策日) 聚合，再跨日求统计 —— 这是 t 值不被放大的关键
    buckets: dict[tuple, dict[str, list]] = {}
    for chain, hz, asof, net, hit in rows:
        tool = ""
        try:
            import json
            arr = json.loads(chain or "[]")
            tool = "+".join(str(x) for x in arr) if arr else ""
        except Exception:  # noqa: BLE001
            tool = str(chain or "")
        buckets.setdefault((tool, int(hz)), {}).setdefault(asof, []).append((net, hit))

    out = []
    for (tool, hz), by_day in buckets.items():
        daily = [statistics.fmean([x[0] for x in v]) for v in by_day.values()]
        hits = [x[1] for v in by_day.values() for x in v]
        n = len(hits)
        out.append({
            "tool": tool, "horizon": hz, "days": len(by_day), "n": n,
            "excess_pp": round(statistics.fmean(daily) * 100, 3) if daily else None,
            "win_rate": round(100.0 * sum(hits) / n, 1) if n else None,
            "t": round(tstat(daily), 2) if tstat(daily) is not None else None,
        })
    out.sort(key=lambda r: -abs(r["t"] or 0))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T08 到期打分")
    ap.add_argument("--min-days", type=int, default=10, help="决策日少于 N 天不列出")
    ap.add_argument("--status", default="BT", help="BT=回测样本；LIVE=线上决策")
    a = ap.parse_args(argv)

    if not os.path.exists(APP_DB):
        print("找不到 viewer/app.db")
        return 1
    con = sqlite3.connect("file:%s?mode=ro" % APP_DB.replace("\\", "/"), uri=True)
    try:
        rows = score(con, a.status)
        live = con.execute("SELECT COUNT(*) FROM tool_decision_log WHERE status='LIVE'").fetchone()[0]
        tot_d = con.execute("SELECT COUNT(*) FROM tool_decision_log WHERE status=?", (a.status,)).fetchone()[0]
        tot_o = con.execute(
            "SELECT COUNT(*) FROM tool_outcome o JOIN tool_decision_log d"
            " ON d.decision_id=o.decision_id WHERE d.status=?", (a.status,)).fetchone()[0]
    finally:
        con.close()

    print("账本：%d 条决策 / %d 条到期结果（status=%s）；线上真实决策 %d 条"
          % (tot_d, tot_o, a.status, live))
    print()
    print("%-34s %5s %6s %7s %9s %8s %7s" % ("工具", "持有", "决策日", "样本", "净超额pp", "胜率%", "t"))
    print("-" * 88)
    for r in rows:
        if r["days"] < a.min_days:
            continue
        flag = ""
        if r["t"] is not None and abs(r["t"]) >= 2:
            flag = "  ← 显著为负" if r["excess_pp"] < 0 else "  ← 显著为正"
        print("%-34s %5d %6d %7d %9.2f %8.1f %7.2f%s"
              % (r["tool"][:34], r["horizon"], r["days"], r["n"],
                 r["excess_pp"] or 0.0, r["win_rate"] or 0.0, r["t"] or 0.0, flag))
    if not rows:
        print("（还没有到期样本：先跑 router_backtest.py --log-decisions）")
    _cost = int(round(_P.BACKTEST_FEE_ROUNDTRIP * 10000.0)) if _P else 13
    print("\n口径：超额已扣 %dbp 双边成本（默认取内核统一值；与回测落库口径一致）；"
          "t 按决策日聚合，不按票堆样本。" % _cost)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
