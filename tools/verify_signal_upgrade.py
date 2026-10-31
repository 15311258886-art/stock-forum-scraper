# -*- coding: utf-8 -*-
"""验证「大样本证据 + 收益维度」改造是否真的改善了排序（2026-10-29）

## 做法

拿已结算的历史预测（prediction_item）当测试集，用两套权重分别重算排序，
比较 top-N 的次日命中率、平均收益与基准超额：

    旧口径 = prediction_item.hit_signals 里当时的 weight（实盘命中率单维度产物）
    新口径 = rescore 后 predictor_score.weight（已叠加收益维度 + 大样本证据）

## 为什么是"重算"而不是"重跑引擎"

重跑引擎需要当时的全量行情快照，而历史快照不完整（policy/overseas 等都是快照语义）。
但 prediction_item.hit_signals 记录了每只票命中的每一路信号及其当时的 pct，
足以复现「信号加权求和」这一段。板块加成、动量冷却两段两套口径同样缺失，
不构成对新旧对比的偏袒。

## 诚实的边界

样本只有几百条，单日 20 只票。这个量级做不出统计显著性，只能看**方向是否一致**。
结果若为"新不一定更好"，必须照实说 —— 改造的价值在于口径更可解释（每条提示都能
说明依据与样本量），而不是保证立刻提高命中率。
"""
import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# predictor 里有 `import kbase`，而 kbase 位于 viewer/ 下（server 启动时也做了同样的插入），
# 缺这一行脚本会在 import 阶段就 ModuleNotFoundError。
_VIEWER = os.path.join(ROOT, "viewer")
if _VIEWER not in sys.path:
    sys.path.insert(0, _VIEWER)


def _f(v, d=0.0):
    try:
        return d if v is None else float(v)
    except (TypeError, ValueError):
        return d


def load_items(con, fwd=1, min_n=1):
    """取已结算的历史预测。返回 [{run_date, symbol, name, hits, ret, bench}]。"""
    col_r, col_b = "ret_t%d" % fwd, "bench_t%d" % fwd
    rows = con.execute(
        "SELECT run_date, symbol, name, hit_signals, %s, %s FROM prediction_item "
        "WHERE %s IS NOT NULL AND hit_signals IS NOT NULL "
        "ORDER BY run_date, rank" % (col_r, col_b, col_r)).fetchall()
    out = []
    for r in rows:
        try:
            hs = json.loads(r[3]) if isinstance(r[3], str) else (r[3] or [])
        except (ValueError, TypeError):
            continue
        if not isinstance(hs, list) or not hs:
            continue
        out.append({"run_date": r[0], "symbol": r[1], "name": r[2], "hits": hs,
                    "ret": _f(r[4]), "bench": _f(r[5])})
    return out


def score_of(hits, weights, use_old):
    """复现「信号加权 + 多信号共振加成」。use_old=True 时用 hits 里记录的当时权重。"""
    s = 0.0
    for h in hits:
        k = h.get("key")
        p = _f(h.get("pct"))
        w = _f(h.get("weight")) if use_old else _f(weights.get(k), _f(h.get("weight")))
        if p <= 0:
            continue
        s += w * p
    if len(hits) >= 2:
        s += 0.10 * (len(hits) - 1)
    return s


def evaluate(items, weights, use_old, topn=10):
    """按日期分组，取 top-N，统计命中率 / 平均收益 / 平均超额。"""
    by_date = {}
    for it in items:
        by_date.setdefault(it["run_date"], []).append(it)
    tot, hit, rets, excess = 0, 0, [], []
    per_day = []
    for dt, arr in sorted(by_date.items()):
        ranked = sorted(arr, key=lambda x: -score_of(x["hits"], weights, use_old))[:topn]
        if not ranked:
            continue
        n = len(ranked)
        h = sum(1 for x in ranked if x["ret"] > 0)
        avg = sum(x["ret"] for x in ranked) / n
        exc = sum(x["ret"] - x["bench"] for x in ranked) / n
        tot += n
        hit += h
        rets.extend(x["ret"] for x in ranked)
        excess.extend(x["ret"] - x["bench"] for x in ranked)
        per_day.append((dt, n, h, avg, exc))
    if not tot:
        return None
    return {
        "n": tot, "hit": hit, "hit_rate": hit / tot,
        "avg_ret": sum(rets) / len(rets),
        "avg_excess": sum(excess) / len(excess),
        "days": len(per_day), "per_day": per_day,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fwd", type=int, default=1)
    ap.add_argument("--topn", type=int, default=10)
    a = ap.parse_args()

    from viewer import predictor as P

    db = os.path.join(ROOT, "viewer", "app.db")
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row

    items = load_items(con, a.fwd)
    if not items:
        print("没有已结算的历史预测（T+%d），无法对比。" % a.fwd)
        return
    new_w = P.load_weights(con)
    old = evaluate(items, new_w, True, a.topn)
    new = evaluate(items, new_w, False, a.topn)
    con.close()

    if not old or not new:
        print("评估失败：样本为空。")
        return

    print("=" * 74)
    print("信号权重改造验证 ｜ T+%d ｜ 每日取 top%d ｜ 样本 %d 条 / %d 个交易日"
          % (a.fwd, a.topn, old["n"], old["days"]))
    print("=" * 74)
    print("%-22s %10s %10s %10s" % ("口径", "命中率", "平均收益", "平均超额"))
    print("-" * 74)
    for name, r in (("旧（命中率单维度）", old), ("新（+收益+大样本证据）", new)):
        print("%-22s %9.1f%% %9.2f%% %9.2f%%"
              % (name, r["hit_rate"] * 100, r["avg_ret"] * 100, r["avg_excess"] * 100))
    print("-" * 74)
    dh = (new["hit_rate"] - old["hit_rate"]) * 100
    dr = (new["avg_ret"] - old["avg_ret"]) * 100
    de = (new["avg_excess"] - old["avg_excess"]) * 100
    print("差值：命中率 %+.1fpt ｜ 平均收益 %+.2fpt ｜ 平均超额 %+.2fpt" % (dh, dr, de))

    # 逐日看方向是否一致（比平均值更能说明问题）
    same = sum(1 for a1, b1 in zip(old["per_day"], new["per_day"]) if b1[4] >= a1[4])
    print("逐日：新口径超额不低于旧口径的天数 %d/%d" % (same, len(old["per_day"])))
    print()
    print("注意：单日仅 top%d，总样本 %d 条，做不出统计显著性 —— 只看方向，"
          "不足以宣称\"必然更好\"。真正的价值在于每条提示现在都能说明依据与样本量。"
          % (a.topn, old["n"]))
    print()
    print("--- 逐日明细（旧 → 新，按平均超额）---")
    for (d0, n0, h0, a0, e0), (d1, n1, h1, a1, e1) in zip(old["per_day"], new["per_day"]):
        print("  %s  旧 %d/%d 超额%+6.2f%%  →  新 %d/%d 超额%+6.2f%%  %s"
              % (d0, h0, n0, e0 * 100, h1, n1, e1 * 100, "↑" if e1 > e0 else ("—" if e1 == e0 else "↓")))


if __name__ == "__main__":
    main()
