"""全库覆盖审计（2026-10-30 建）—— 回答一个问题：可预测的股票到底漏在哪一层。

用法：
    python tools/audit_coverage.py            # 快：只查库与池子，不跑预测
    python tools/audit_coverage.py --deep     # 深：额外实测各模块实际产出只数

背景（BUG-20261030）：进哥多次反馈「5000 多只票，怎么老从几百只里选」。
排查发现 strategy_sim.universe() 的主路径读 stock_liquidity 表（本机从未建过），
异常被吞后走「positions ∪ lhb_stocks」兜底，而 positions 清理后为 0 行、
lhb_stocks 只剩 703 只历史龙虎榜票 —— 候选池被永久锁在 594~734 只，
POOL_LIMIT 从 160 调到 2000 完全没生效（prediction_run.pool_n 铁证：13 天恒为 157）。

本脚本把「日线全库 → 干净宇宙 → 各模块池子 → 实际产出」逐层打出，
每层标注数量与丢失原因，用于每次改池子逻辑后回归。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# selection / predictor 等业务模块在 viewer/ 下，需单独加入搜索路径
VIEWER = os.path.join(ROOT, "viewer")
if VIEWER not in sys.path:
    sys.path.insert(0, VIEWER)

APP_DB = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")
KLINE_DB = os.path.join(ROOT, "data", "kline_daily.db")
KBASE_DB = os.path.join(ROOT, "viewer", "kbase.db")


def _ro(path):
    if not os.path.exists(path):
        return None
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def _is_bj(code: str) -> bool:
    return code[:2] == "92" or code[:1] in ("4", "8")


def _is_st(name: str) -> bool:
    u = (name or "").upper().replace(" ", "")
    return ("ST" in u) or ("退" in u)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true", help="额外实测各模块实际产出")
    args = ap.parse_args()

    app = _ro(APP_DB)
    kl = _ro(KLINE_DB)
    if app is None or kl is None:
        print("FAIL 数据库缺失：app.db 或 kline_daily.db")
        return 1

    names = dict(app.execute("SELECT code, name FROM stock_names"))

    print("=" * 78)
    print("第 0 层 · 原始宇宙（谁存在）")
    print("=" * 78)
    last = kl.execute("SELECT MAX(d) FROM kline").fetchone()[0]
    k_all = kl.execute("SELECT COUNT(DISTINCT code) FROM kline").fetchone()[0]
    k_day = kl.execute("SELECT COUNT(DISTINCT code) FROM kline WHERE d=?",
                       (last,)).fetchone()[0]
    print("  kline 全库       %5d 只（历史出现过），最新交易日 %s 在册 %d 只"
          % (k_all, last, k_day))
    print("  stock_names      %5d 只（名称字典）"
          % app.execute("SELECT COUNT(*) FROM stock_names").fetchone()[0])

    print()
    print("=" * 78)
    print("第 1 层 · 干净宇宙（剔 ST / 北交所 / 无K线）")
    print("=" * 78)
    from viewer.universe_filter import all_ashares, get_exclusion_pool
    a = set(all_ashares())
    ex, _, _ = get_exclusion_pool()
    clean = a - ex
    bj = {c for c in clean if _is_bj(c)}
    usable = clean - bj
    st_n = sum(1 for c in ex if _is_st(names.get(c, "")))
    print("  名称字典 A 股格式   %5d 只" % len(a))
    print("  剔除池（ST/无K线）  %5d 只  其中 ST/退 %d 只" % (len(ex), st_n))
    print("  干净宇宙            %5d 只" % len(clean))
    print("  再剔北交所(4/8/92)  %5d 只  ← 这是「可用宇宙」" % len(usable))

    print()
    print("=" * 78)
    print("第 2 层 · 各模块实际使用的池子")
    print("=" * 78)
    import strategy_sim as SS
    try:
        from viewer.config import PILLARS_POOL_LIMIT
    except Exception:  # noqa: BLE001
        PILLARS_POOL_LIMIT = -1
    c1, _, _ = SS.universe(PILLARS_POOL_LIMIT if PILLARS_POOL_LIMIT > 0 else 2000)
    c2, _, _ = SS.universe(6000)
    print("  %-34s %5d 只  (%.1f%% of 可用宇宙)"
          % ("predictor 重分析池 (POOL_LIMIT=%s)" % PILLARS_POOL_LIMIT,
             len(c1), 100.0 * len(c1) / max(1, len(usable))))
    print("  %-34s %5d 只  (%.1f%%)"
          % ("universe(6000) 理论上限", len(c2), 100.0 * len(c2) / max(1, len(usable))))

    def cnt(sql, *p):
        try:
            return app.execute(sql, p).fetchone()[0]
        except Exception:  # noqa: BLE001
            return -1

    print("  %-34s %5d 只" % ("universe_scan 轻扫描当日",
                              cnt("SELECT COUNT(*) FROM stock_universe_scan WHERE updated=("
                                  "SELECT MAX(updated) FROM stock_universe_scan)")))
    print("  %-34s %5d 只" % ("stock_indicators_daily 当日",
                              cnt("SELECT COUNT(DISTINCT code) FROM stock_indicators_daily "
                                  "WHERE date=(SELECT MAX(date) FROM stock_indicators_daily)")))
    print("  %-34s %5d 只  ← 四支柱基本面依赖"
          % ("fundamentals 快照", cnt("SELECT COUNT(*) FROM fundamentals")))
    print("  %-34s %5d 只" % ("stock_holders 股东", cnt("SELECT COUNT(DISTINCT symbol) FROM stock_holders")))
    print("  %-34s %5d 只" % ("sel_pool 低波池(入库)",
                              cnt("SELECT COUNT(*) FROM sel_pool WHERE asof=("
                                  "SELECT MAX(asof) FROM sel_pool)")))

    print()
    print("=" * 78)
    print("第 3 层 · 指标表缺口构成（指标表 vs 日线当日）")
    print("=" * 78)
    i_day = {x[0] for x in app.execute(
        "SELECT DISTINCT code FROM stock_indicators_daily WHERE date=("
        "SELECT MAX(date) FROM stock_indicators_daily)")}
    k_set = {x[0] for x in kl.execute(
        "SELECT DISTINCT code FROM kline WHERE d=?", (last,))}
    miss = sorted(k_set - i_day)
    m_st = [c for c in miss if _is_st(names.get(c, ""))]
    m_bj = [c for c in miss if _is_bj(c)]
    m_new = [c for c in miss if c not in m_st and c not in m_bj]
    print("  日线在册 %d 只 / 指标表 %d 只 / 缺 %d 只" % (len(k_set), len(i_day), len(miss)))
    print("    其中 ST/退      %4d 只（设计内剔除）" % len(m_st))
    print("    其中 北交所      %4d 只（设计内剔除）" % len(m_bj))
    print("    其中 新股/缺指标 %4d 只（指标需要 200 根日线，新股天然算不出）" % len(m_new))
    if m_new:
        print("      样本：%s" % ", ".join("%s %s" % (c, names.get(c, "?")) for c in m_new[:6]))
    unexpected = [c for c in m_new if names.get(c)]
    ok = len(unexpected) <= max(60, int(0.02 * len(k_set)))
    print("  判定：%s" % ("PASS 缺口全部可由 ST/北交所/新股解释" if ok
                        else "FAIL 存在 %d 只无法解释的缺口" % len(unexpected)))

    print()
    print("=" * 78)
    print("第 4 层 · 低波池截断检查（POOL_MAX）")
    print("=" * 78)
    try:
        import selection as SEL
        raw = SEL.count_pool_candidates(app)
        print("  满足入池条件    %5d 只" % raw.get("n", -1))
        print("  入库（截断后）  %5d 只   POOL_MAX=%s" % (raw.get("saved", -1), SEL.POOL_MAX))
        cap = raw.get("n", 0) > raw.get("saved", 0)
        print("  判定：%s" % ("WARN 有 %d 只符合条件却没入库（截断）"
                            % (raw.get("n", 0) - raw.get("saved", 0)) if cap else "PASS 未截断"))
    except Exception as e:  # noqa: BLE001
        print("  跳过：%s" % str(e)[:120])

    print()
    print("=" * 78)
    print("第 5 层 · 预测历史池子（prediction_run.pool_n）")
    print("=" * 78)
    rows = app.execute("SELECT run_date, pool_n FROM prediction_run "
                       "ORDER BY run_date DESC LIMIT 8").fetchall()
    for d, n in rows:
        # 2026-10-31 整改（测评 P0-D）：这里的 800 原本是拍出来的绝对值，
        # 而下面那行判定用的是「占可用宇宙 25%」——同一个问题两个口径，
        # 池子从 5,000 涨到 5,300 之后，800 这条线就既不触发也不说明任何事。
        # 改成同源：标记条件与判定条件走同一个比例，去掉这个字面量。
        flag = "  ← 疑似锁死" if n and n < 0.25 * len(usable) else ""
        print("  %s  %5d 只%s" % (d, n or 0, flag))
    # 判定看**最新一条**（修前 594~734 只），不用 max —— 旧数据本就是坏的
    latest = (rows[0][1] or 0) if rows else 0
    print("  判定：%s" % ("PASS 最新一天池子 %d 只，与可用宇宙 %d 只量级相符"
                        % (latest, len(usable)) if latest >= 0.25 * len(usable)
                        else "FAIL 最新一天池子仅 %d 只，远小于可用宇宙 %d 只"
                             % (latest, len(usable))))

    if args.deep:
        print()
        print("=" * 78)
        print("第 6 层 · 深测：各模块实际产出")
        print("=" * 78)
        try:
            import predictor as P
            k = {}
            bars = P.fetch_bars(c2, k)
            print("  fetch_bars        %5d 只（本地库直读，零网络）" % len(bars))
        except Exception as e:  # noqa: BLE001
            print("  fetch_bars 失败：%s" % str(e)[:120])

    app.close()
    kl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
