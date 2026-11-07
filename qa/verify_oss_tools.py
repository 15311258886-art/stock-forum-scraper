# -*- coding: utf-8 -*-
"""三期 GitHub 开源规则筛选器 —— 上架前的四项自检（2026-11-04）。

跑法：`.venv/Scripts/python.exe qa/verify_oss_tools.py`

检查什么（每一项对应一条「会出错但很难查」的风险）

  A. **字段口径**：快照里的 ret1 / mom20 / dist_high250 与一条**独立的窗口函数 SQL**
     逐只逐值比对。差一位下标（21 根写成 20 根）不会报错，只会让卡片上的超额
     和实际命中对不上 —— 这是本次最大的技术风险，必须逐值核。
  B. **命中数**：11 条在【最近交易日】的实际命中，与 `oss_rule` 里的 n / days
     （原作参数下的平均每日命中）对照量级。
  C. **与输入池无关**：`oss_mom20` / `oss_burst_vol` / `oss_rsi_overbought` 各算两遍 ——
     「全宇宙命中 ∩ 500 只子集」 必须**完全等于** 「直接用 500 只子集算」。
     这是筛台「每个工具只算一次再求交」的前提，不成立则求交结果是错的。
  D. **参数归一**：同一组参数写成 `1` / `1.0` 两种形态，`toolbox_stack` 必须给出同一个
     缓存签名（否则前端点一下永远不命中，用户白等）。

⚠️ 这个脚本连的是**生产库** `viewer/app.db`：它要 `seed()`（否则 `apply_tool` 找不到登记行）
   并会往 `tool_run_log` 写留痕。因此跑完之后 `tool_registry` 里会多出 11 行 oss_*。
   如果此时**服务还没重启**（进程里没有这 11 个工具的 apply），页面会显示 11 张
   「已登记但还没有实现」的坏卡 —— 跑完请执行一次：
       DELETE FROM tool_registry WHERE tool_key LIKE 'oss%';
   重启后 seed() 会自动重新插入（幂等），不需要手工补。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from viewer import toolbox as tb                     # noqa: E402
from viewer import toolbox_stack as stack            # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "viewer", "app.db")

# 11 条规则 → (tool_key, oss_rule.key, 原作参数说明)
RULES = [
    ("oss_ma_align", "minervini_trend", "均线多头/不剔涨停"),
    ("oss_near_high", "minervini_near_high", "25%"),
    ("oss_near_high_tight", "minervini_breakout_tight", "5%"),
    ("oss_burst_vol", "stockbee_burst", "4%+1.5倍"),
    ("oss_burst", "stockbee_burst_novol", "4%"),
    ("oss_pullback_ma20", "pullback_ma20", "-3%~0（原作只设下界）"),
    ("oss_shrink_vol", "shrink_volume", "0.7"),
    ("oss_mom20", "momentum_20", "mom20>=20"),
    ("oss_reversal20", "reversal_20", "mom20<=-20"),
    ("oss_high_vol", "high_vol", "6%"),
    ("oss_rsi_overbought", "rsi_overbought", "80"),
]

_REF_SQL = """
WITH t AS (
  SELECT code, date, close,
    LAG(close,1)  OVER w AS c1,
    LAG(close,21) OVER w AS c21,
    MAX(close) OVER (PARTITION BY code ORDER BY date
                     ROWS BETWEEN 250 PRECEDING AND CURRENT ROW) AS hi
  FROM stock_indicators_daily WHERE date>=? AND close>0
  WINDOW w AS (PARTITION BY code ORDER BY date)
)
SELECT code, close, c1, c21, hi FROM t WHERE date=?
"""


def _pct(cur, prev):
    if not cur or not prev or prev <= 0:
        return None
    return (cur / prev - 1.0) * 100.0


def check_a(con, latest: str) -> bool:
    """字段口径逐值比对（独立 SQL 窗口函数 vs 快照构造）。"""
    print("\n=== A. 字段口径逐值比对（独立 SQL 重算 vs 快照构造） ===")
    cal = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily WHERE date<=? "
        "ORDER BY date DESC LIMIT ?", (latest, 251))]
    since = cal[-1]
    t0 = time.time()
    rows = con.execute(_REF_SQL, (since, latest)).fetchall()
    print("  参考口径算完：%d 只 / %.1f 秒（这条 SQL 慢，是故意的 —— 它不共用快照任何代码）"
          % (len(rows), time.time() - t0))
    _, snap = tb.oss_snapshot(con)
    bad = {"ret1": 0, "mom20": 0, "dist_high250": 0, "missing": 0}
    samples = []
    for code, close, c1, c21, hi in rows:
        r = snap.get(str(code))
        if r is None:
            bad["missing"] += 1
            continue
        for tag, mine, ref in (
                ("ret1", r.ret1, _pct(close, c1)),
                ("mom20", r.mom20, _pct(close, c21)),
                ("dist_high250", r.dist_high250, _pct(close, hi))):
            if mine is None and ref is None:
                continue
            if mine is None or ref is None or abs(mine - ref) > 1e-6:
                bad[tag] += 1
                if len(samples) < 6:
                    samples.append((code, tag, mine, ref))
    print("  不一致计数：%s" % bad)
    for s in samples:
        print("    样本 %s：%s 我方=%s 参考=%s" % s)
    ok = not any(bad.values())
    print("  结论：%s（%d 只逐值一致）" % ("PASS" if ok else "FAIL", len(rows) - bad["missing"]))
    return ok


def check_b(con, latest: str, universe: list) -> dict:
    """11 条的实际命中 + 与 oss_rule 的平均每日命中对照。"""
    print("\n=== B. 命中数（数据日 %s，宇宙 %d 只） ===" % (latest, len(universe)))
    ref = {}
    for r in con.execute("SELECT key,n,days,excess,t,status FROM oss_rule"):
        ref[str(r[0])] = {"n": r[1], "days": r[2], "excess": r[3], "t": r[4], "status": r[5]}
    out = {}
    print("  %-22s %6s %8s %10s %7s  %s" % ("tool_key", "命中", "原作日均", "偏差", "耗时ms", "原作/建议怎么调"))
    for key, okey, note in RULES:
        d = dict(tb.get_tool(con, key)["default_params"])
        t0 = time.time()
        res = tb.apply_tool(con, key, universe, d)
        ms = int((time.time() - t0) * 1000)
        hits = len(res.get("kept") or [])
        n, days = (ref.get(okey) or {}).get("n") or 0, (ref.get(okey) or {}).get("days") or 0
        avg = (n / days) if days else 0.0
        dev = ((hits - avg) / avg * 100.0) if avg else 0.0
        out[key] = {"hits": hits, "avg": round(avg, 1), "dev": round(dev, 1), "ms": ms,
                    "ok": bool(res.get("ok")), "error": res.get("error")}
        print("  %-22s %6d %8.1f %9.1f%% %7d  %s"
              % (key, hits, avg, dev, ms, note))
        if not res.get("ok"):
            print("     !! 执行失败：%s" % res.get("error"))
    return out


def check_c(con, universe: list) -> bool:
    """与输入池无关：全宇宙 ∩ 子集 == 直接算子集。"""
    print("\n=== C. 与输入池无关（求交前提） ===")
    sub = universe[:500]
    ok_all = True
    for key in ("oss_mom20", "oss_burst_vol", "oss_rsi_overbought"):
        d = dict(tb.get_tool(con, key)["default_params"])
        full = set(tb.apply_tool(con, key, universe, d).get("kept") or [])
        direct = set(tb.apply_tool(con, key, sub, d).get("kept") or [])
        inter = full & set(sub)
        same = inter == direct
        ok_all = ok_all and same
        print("  %-22s 全宇宙命中 %5d · 子集直算 %4d · 交集 %4d → %s"
              % (key, len(full), len(direct), len(inter), "相等" if same else "不相等 !!"))
        if not same:
            print("     只在直算里：%s" % sorted(direct - inter)[:5])
            print("     只在交集里：%s" % sorted(inter - direct)[:5])
    print("  结论：%s" % ("PASS" if ok_all else "FAIL"))
    return ok_all


def check_d(con) -> bool:
    """参数归一：1 / 1.0 必须是同一个缓存签名。"""
    print("\n=== D. 参数归一（1 vs 1.0） ===")
    ok = True
    for key in ("oss_ma_align", "oss_rsi_overbought", "oss_burst_vol"):
        t = tb.get_tool(con, key)
        a = stack._merge_params(t, dict(t["default_params"]))
        b = stack._merge_params(t, {k: (float(v) if isinstance(v, int) else v)
                                   for k, v in t["default_params"].items()})
        same = stack.tool_digest(key, a) == stack.tool_digest(key, b)
        ok = ok and same
        print("  %-22s 原样=%s 浮点=%s → %s"
              % (key, a, b, "同签名" if same else "签名不同 !!"))
    print("  结论：%s" % ("PASS" if ok else "FAIL"))
    return ok


def main() -> int:
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    con.execute("PRAGMA query_only=1")
    # 只读连接不能 seed，但 apply_tool 需要 tool_registry 里有登记行 → 用可写连接
    con.close()
    con = sqlite3.connect(DB)
    tb.ensure_tables(con)
    tb.seed(con)
    latest = str(con.execute("SELECT MAX(date) FROM stock_indicators_daily").fetchone()[0])
    universe = [str(c) for c in tb.universe_codes(con)]
    print("宇宙 %d 只 · 指标表最近交易日 %s" % (len(universe), latest))

    t0 = time.time()
    _, snap = tb.oss_snapshot(con)
    print("快照冷启动：%d 只 / %.2f 秒" % (len(snap), time.time() - t0))

    a = check_a(con, latest)
    b = check_b(con, latest, universe)
    c = check_c(con, universe)
    d = check_d(con)
    print("\n汇总：A=%s B(全部执行)=%s C=%s D=%s"
          % ("PASS" if a else "FAIL",
             "PASS" if all(v["ok"] for v in b.values()) else "FAIL",
             "PASS" if c else "FAIL", "PASS" if d else "FAIL"))
    return 0 if (a and c and d and all(v["ok"] for v in b.values())) else 1


if __name__ == "__main__":
    sys.exit(main())
