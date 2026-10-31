# -*- coding: utf-8 -*-
"""对 /api/today_decision 与预测引擎做一次真实耗时剖析（只读，不写库）。

背景：2026-10-30 深夜实测 today_decision 52s、prediction/today 与 scores 均 >25s，
而相邻接口全在 0.1s 级 —— 说明是这三个聚合函数内部的某几段在扫大表，
不是全站被拖。这里用 cProfile 把「哪一段吃掉了 50 秒」量出来。

用法：python prof_today_decision.py [名字]
     名字 ∈ {today, pred_today, pred_scores}，默认 today。
"""
import cProfile
import io
import pstats
import sys
import time

sys.path.insert(0, ".")

import viewer.server as S  # noqa: E402


def _run(name):
    fn = {
        "today": S.api_today_decision,
        "pred_today": S.api_prediction_today,
        "pred_scores": S.api_prediction_scores,
    }[name]
    pr = cProfile.Profile()
    t0 = time.time()
    pr.enable()
    try:
        res = fn()
    finally:
        pr.disable()
    dt = time.time() - t0
    print("\n==== %s 实耗 %.1fs ====" % (name, dt))
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(28)
    out = s.getvalue()
    # 只保留函数级摘要，去掉表头噪声
    for ln in out.splitlines():
        if "{method" in ln or "built-in" in ln or "ncalls" in ln or "Ordered by" in ln:
            continue
        if ln.strip().startswith("viewer.") or ln.strip().startswith("board") or ln.strip().startswith("sqlite"):
            print(ln)
    return res


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "today"
    _run(name)
