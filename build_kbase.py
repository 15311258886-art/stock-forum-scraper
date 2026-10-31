# -*- coding: utf-8 -*-
"""全市场 K 线沉淀构建脚本（一次性 / 可重跑 / 可增量）。

把 stock_names 中 6 位 A 股代码（当前约 5237 只，运行期计算）的日 K 从上市日至今，
经 Sina 抓取并沉淀到 viewer/kbase.db（SQLite）。旧的 JSON 缓存（predict_kcache.json /
kline_cache.json）会自动并入，不重复抓。

完备性口径：某票"末根日期 == 数据集最新交易日"才算完整（日期口径，非根数阈值）。
停牌/退市/源站停更的票经重抓确认末根不变后，登记进 suspended 表，后续跳过不再重试。

用法：
    python build_kbase.py                  # 全量构建（仅抓末根!=最新的票 + 新增）
    python build_kbase.py --reset          # 先清空 kbase.db 再构建
    python build_kbase.py --incremental    # 每日增量：只补末根陈旧的票 + 新增代码

注意：Sina 限流敏感，并发默认 2（宁可慢点也要全量真实数据）。全量约 5237 只 ×
一次请求/只，约 8~15 分钟；失败/停牌代码记录在返回里，可重复运行补齐。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))


def load_codes() -> list:
    db = os.path.join(ROOT, "viewer", "app.db")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cs = [str(r[0]) for r in con.execute("SELECT code FROM stock_names")]
    con.close()
    # 仅保留 6 位 A 股代码（与 Sina prefix 逻辑一致：sh/sz/bj）
    return [c for c in cs if len(c) == 6 and c.isdigit() and c[0] in "03648"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="先清空 kbase.db 再构建")
    ap.add_argument("--incremental", action="store_true",
                    help="每日增量：只补末根!=最新的票 + 新增代码（停牌股已豁免跳过）")
    ap.add_argument("--workers", type=int, default=2,
                    help="并发数；Sina 限流敏感，默认 2 避免返回被截断（宁可慢点也要全量）")
    ap.add_argument("--datalen", type=int, default=10000)
    args = ap.parse_args()

    from viewer import kbase

    if args.reset and not args.incremental:
        try:
            os.remove(kbase.DB)
            print("[build_kbase] 已清空旧 kbase.db", flush=True)
        except FileNotFoundError:
            pass

    b = kbase.get_base()
    t0 = time.time()

    def prog(done, total, stats):
        print(f"  ..{done}/{total}  ok={stats['ok']} fail={stats['fail']} "
              f"skip={stats['skipped']} susp={stats.get('suspended', 0)}", flush=True)

    if args.incremental:
        print(f"[build_kbase] 增量更新模式，目标来自 app.db stock_names", flush=True)
        res = b.incremental_update(datalen=args.datalen, workers=args.workers, progress=prog)
    else:
        codes = load_codes()
        print(f"[build_kbase] 目标全市场 {len(codes)} 只，开始抓取并沉淀到 {kbase.DB}", flush=True)
        res = b.bulk_fetch_all(
            codes, datalen=args.datalen, workers=args.workers,
            delay=0.05, progress=prog,
        )
    print(f"[build_kbase] 完成 用时 {time.time()-t0:.0f}s  "
          f"最终 codes={b.count()} rows={b.row_count()} fail={res['fail']}", flush=True)
    if res["fail"]:
        print("  FAIL(前50):", res.get("fail_codes"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
