# -*- coding: utf-8 -*-
"""公司概况批量预热（2026-09-26 新增）

把库中出现的 A 股标的的「公司概况」（主营业务 / 公司简介 / 所属行业）批量抓取并落库，
这样点开任意一只股票时「重要信息」底部的公司主营块都是秒出，且断网也能看。

数据源：东方财富 F10 公司概况（RPT_F10_BASIC_ORGINFO），与 /api/quote 的 org 字段同源。

用法：
  python warm_org.py                # 预热库内全部 A 股标的（已落库的自动跳过）
  python warm_org.py --force        # 全部重抓（公司更名/主业变更后用）
  python warm_org.py --top 50       # 只预热提及次数最高的 50 只
  python warm_org.py --workers 6    # 并发数（默认 6，抓取在子线程、落库在主线程串行）
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer.server import DB, _em_org_info, _em_secid  # noqa: E402

A_SHARE = re.compile(r"^\d{6}$")


def targets(top: int | None = None) -> list[str]:
    """库内出现过的标的，按「被提及次数 + 持仓/交易记录数」降序。仅保留 A 股 6 位代码。"""
    rows = DB.conn.execute(
        "SELECT symbol, COUNT(*) n FROM stock_mentions WHERE symbol IS NOT NULL AND symbol<>'' "
        "GROUP BY symbol "
        "UNION ALL SELECT symbol, 1 FROM positions WHERE symbol IS NOT NULL AND symbol<>'' "
        "UNION ALL SELECT symbol, 1 FROM trades WHERE symbol IS NOT NULL AND symbol<>''"
    ).fetchall()
    score: dict[str, int] = {}
    for r in rows:
        s = (r["symbol"] or "").strip()
        if not A_SHARE.match(s):          # 美股/港股代码不在此列（详情抽屉只覆盖 A 股）
            continue
        score[s] = score.get(s, 0) + int(r["n"] or 0)
    ordered = [s for s, _ in sorted(score.items(), key=lambda kv: -kv[1])]
    return ordered[:top] if top else ordered


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="已落库的也重新抓")
    ap.add_argument("--top", type=int, default=0, help="只处理前 N 只（默认全部）")
    ap.add_argument("--workers", type=int, default=6, help="并发抓取数")
    a = ap.parse_args()

    codes = targets(a.top or None)
    if not a.force:
        have = {r["code"] for r in DB.conn.execute("SELECT code FROM stock_org")}
        codes = [c for c in codes if c not in have]
    print(f"[预热] 待抓取 {len(codes)} 只 A 股（并发 {a.workers}）", flush=True)
    if not codes:
        print("[预热] 无需处理：库内标的均已就绪。")
        return 0

    def work(code: str):
        market, c, _secid = _em_secid(code)
        try:
            return code, _em_org_info(c, market)
        except Exception as e:  # noqa: BLE001 —— 单只失败不影响整批
            print(f"    ! {code} 抓取异常：{type(e).__name__}", flush=True)
            return code, None

    t0 = time.time()
    done = fail = 0
    misses: list[str] = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, (code, info) in enumerate(ex.map(work, codes), 1):
            if info:
                try:
                    DB.org_put(code, info)      # 落库在主线程串行，避免并发写锁
                    done += 1
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    print(f"    ! {code} 落库失败：{type(e).__name__}", flush=True)
            else:
                fail += 1
                misses.append(code)
            if i % 25 == 0 or i == len(codes):
                print(f"    {i}/{len(codes)}  成功 {done}  失败 {fail}  用时 {time.time() - t0:.0f}s",
                      flush=True)
    print(f"[预热] 完成：成功 {done} / 失败 {fail} / 共 {len(codes)}，耗时 {time.time() - t0:.0f}s")
    if misses:
        print(f"[预热] 未取到（多为退市/停牌/非 A 股代码）：{' '.join(misses[:20])}"
              + (" …" if len(misses) > 20 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
