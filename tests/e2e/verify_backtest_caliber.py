"""verify_backtest_caliber.py —— 历史回测板块的【口径等价性】守卫（后端，只读）

为什么必须有它：
    历史回测板块自己算 T+1/T+3/T+5/T+10/T+20。如果它的算法与引擎（predictor）
    的口径有一丝不一致，同一天的"收益/命中"在不同页面会给出不同数字（口径漂移），
    进哥就无法信任任何一边。本脚本把口径钉死：只要有人在任一侧改了公式，立刻 FAIL。

2026-10-15 变更（进哥拍板）：历史回测**只留四支柱**，旧「主引擎预测」数据源已撤。
    因此本守卫同步改为「四支柱口径」：
      A 数据源范围：payload 只暴露 pillars；dates 只来自 pillar_item（n_engine 恒 0）
      B/C/D 口径等价：
        B 逐股收益 —— 用 predictor 的底层函数独立复算，与接口值 1e-9 内一致
        C 基准     —— 独立复算 + 与 prediction_run.bench_json（引擎已入库值）比对（该日有则比）
        D 命中     —— 必须严格等于「ret > bench」
      E 诚实性：三状态自洽（pending 不得有 ret 且 need>0；nodata 不得有 ret；ok 必须有 ret）
      F T+20 存在且当前应为未到期
      G 默认必须落在「有已到期样本」的一期（否则打开是空面板）
      H 家底：source_stats 只有 pillars，且期数与 pillar_item 的实际日期数一致

用法：.venv\\Scripts\\python.exe verify_backtest_caliber.py
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "viewer"))

import backtest as BT  # noqa: E402
import predictor as P  # noqa: E402
import pillars as PL    # noqa: E402  (E 引擎自加权：聚合口径须 == payload)

DB = os.path.join(ROOT, "viewer", "app.db")
FAILS = []


def ok(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row

# 独立复算用的 K 线底座（与 backtest._context 不同路径，刻意各算一遍）
KC = (P.load_kcache().get("bars") or {})
try:
    _codes, _n, _b = P.SS.universe(P.POOL_LIMIT)
except Exception:  # noqa: BLE001
    _codes = list(KC.keys())
# 与 backtest._context 同口径：只收有 30 根以上 K 线的票（否则基准池成员不同 → bench 必然对不上）
POOL = [c for c in _codes if c in KC and len(KC[c]) >= 30]


def my_ret(code, rd, p):
    k = KC.get(code) or []
    idx = P._bars_index(k).get(rd)
    if idx is None:
        return None
    return P._window_ret(k, idx, p)


def my_bench(rd, p):
    vals = [x for c in POOL if (x := my_ret(c, rd, p)) is not None]
    return (sum(vals) / len(vals)) if vals else None


print("\n[A] 数据源范围（只应有四支柱）")
pay = BT.payload(con)
ok(pay.get("ok") is True, "payload.ok")
for key in ("dates", "periods", "rows", "period_stats", "summary", "asof", "sources"):
    ok(key in pay, "payload 含字段 %s" % key)
ok(list(pay["sources"].keys()) == ["pillars"],
   "数据源只剩四支柱（实测 %s）" % list(pay["sources"].keys()))
ok(list(pay["periods"]) == [1, 3, 5, 10, 20], "周期 = T+1/3/5/10/20，实测 %s" % pay["periods"])
pillar_dates = sorted({r["run_date"] for r in con.execute(
    "SELECT DISTINCT run_date FROM pillar_item")}, reverse=True)
ok([x["date"] for x in pay["dates"]] == pillar_dates,
   "日期清单 == pillar_item 实际日期（%s）" % ",".join(pillar_dates))
ok(all(x["n_engine"] == 0 for x in pay["dates"]), "n_engine 恒为 0（旧主引擎不再计入）")
print("    期数=%d，asof=%s" % (len(pay["dates"]), pay["asof"]))

print("\n[B/C/D] 口径等价性（接口值 vs 独立复算 / 引擎已入库值）")
tot_b = ok_b = tot_d = ok_d = tot_h = ok_h = 0
tot_v = 0            # 与「引擎入库快照」的对照（仅供参考，见下）
for d in pay["dates"]:
    rd = d["date"]
    p_dat = BT.payload(con, date=rd)
    if not p_dat.get("ok"):
        continue
    # C1（硬断言）：接口里的 bench 必须 == 独立复算值（同一份 K 线底座的代码等价性）
    for p in (1, 3, 5, 10):
        c = next((x for r in p_dat["rows"] for x in r["cells"] if x["p"] == p), None)
        if not c or c["bench"] is None:
            continue
        mb = my_bench(rd, p)
        tot_b += 1
        same = mb is not None and abs(mb - c["bench"]) < 1e-9
        ok_b += 1 if same else 0
        if not same:
            print("      bench 不一致 %s t%d 接口=%s 独立算=%s" % (rd, p, c["bench"], mb))
    # C2（参考项）：与引擎当年入库的 bench_json 比 —— 只作对照，不计 FAIL。
    # 原因：bench_json 是**生成当日**的快照；此后本地 K 线缓存重抓（前复权在除权日会整体重算），
    # 基准会有 ~1e-4 量级的漂移。这属于数据底座差异，不是本板块的代码口径问题。
    row = con.execute("SELECT bench_json FROM prediction_run WHERE run_date=?", (rd,)).fetchone()
    if row and row["bench_json"]:
        stored = json.loads(row["bench_json"])
        for p in (1, 3, 5, 10):
            a, b = stored.get("t%d" % p), my_bench(rd, p)
            if a is None or b is None:
                continue
            tot_v += 1
            if abs(a - b) >= 1e-3:
                print("      [参考] 与入库快照偏差较大 %s t%d 快照=%s 现算=%s" % (rd, p, a, b))
    # B/D 逐股：接口 cells vs 独立复算
    for r in p_dat["rows"]:
        for c in r["cells"]:
            p = c["p"]
            mine_r = my_ret(r["symbol"], rd, p)
            if c["status"] == "ok":
                tot_d += 1
                same = mine_r is not None and abs(mine_r - c["ret"]) < 1e-9
                ok_d += 1 if same else 0
                if not same:
                    print("      ret 不一致 %s %s t%d 接口=%s 独立算=%s"
                          % (rd, r["symbol"], p, c["ret"], mine_r))
                mb = my_bench(rd, p)
                exp_hit = 1 if (mb is not None and c["ret"] > mb) else 0
                tot_h += 1
                ok_h += 1 if exp_hit == c["hit"] else 0
                if exp_hit != c["hit"]:
                    print("      hit 不一致 %s %s t%d 接口=%s 期望=%s"
                          % (rd, r["symbol"], p, c["hit"], exp_hit))
ok(tot_b > 0 and ok_b == tot_b, "C 基准等价（接口 vs 独立复算）%d/%d" % (ok_b, tot_b))
print("    （与引擎当年入库快照的对照样本 %d 条，仅参考不计 FAIL，原因见脚本注释）" % tot_v)
ok(tot_d > 0 and ok_d == tot_d, "B 逐股收益等价（vs 独立复算）%d/%d" % (ok_d, tot_d))
ok(tot_h > 0 and ok_h == tot_h, "D 命中等价（hit == ret>bench）%d/%d" % (ok_h, tot_h))

print("\n[E] 诚实性：三状态必须自洽（未到期/无数据都不得假冒有值）")
bad = []
n_pend = n_nod = n_ok = 0
for d in pay["dates"]:
    p_dat = BT.payload(con, date=d["date"])
    if not p_dat.get("ok"):
        continue
    for r in p_dat["rows"]:
        for c in r["cells"]:
            tag = "%s %s t%d" % (d["date"], r["symbol"], c["p"])
            if c["status"] == "pending":
                n_pend += 1
                if c["ret"] is not None:
                    bad.append(tag + " pending 却有 ret")
                if not (c["need"] > 0):
                    bad.append(tag + " pending 但 need<=0")
            elif c["status"] == "nodata":
                n_nod += 1
                if c["ret"] is not None:
                    bad.append(tag + " nodata 却有 ret")
            elif c["status"] == "ok":
                n_ok += 1
                if c["ret"] is None:
                    bad.append(tag + " ok 却无 ret")
                if c["need"] != 0:
                    bad.append(tag + " ok 但 need!=0")
            else:
                bad.append(tag + " 未知状态 " + str(c["status"]))
print("    状态分布：ok=%d pending(未到期)=%d nodata(缺K线)=%d" % (n_ok, n_pend, n_nod))
ok(not bad and n_pend > 0, "三状态自洽（异常 %d 条）%s" % (len(bad), bad[:3]))

print("\n[F] T+20 存在性")
c20 = [c for r in pay["rows"] for c in r["cells"] if c["p"] == 20]
ok(len(c20) > 0, "T+20 周期已提供（%d 个单元格）" % len(c20))
oldest = min(x["date"] for x in pay["dates"])
el = BT._elapsed(oldest)
ok(el < 20, "当前最老预测日 %s 仅走完 %d 个交易日 → T+20 应全为未到期（符合预期）" % (oldest, el))

print("\n[G] 默认必须落在「有已到期样本」的一期（否则打开是空面板）")
ok(pay.get("auto") is not None, "payload 带 auto 标记：%s" % json.dumps(pay.get("auto"), ensure_ascii=False))
ok(pay["summary"]["n"] > 0,
   "默认即命中样本：%s · T+%d · %d 只已到期（命中率 %s）" % (
       pay["date"], pay["period"], pay["summary"]["n"],
       "—" if pay["summary"]["hit_rate"] is None else "%.1f%%" % (pay["summary"]["hit_rate"] * 100)))
_fullest = max(pay["dates"], key=lambda x: (x["elapsed"], x["date"]))
ok(pay["date"] == _fullest["date"],
   "默认日期 = 走完交易日最多的那一期（%s / %d 日）" % (_fullest["date"], _fullest["elapsed"]))

print("\n[H] 数据源家底 source_stats")
ss = pay.get("source_stats") or {}
ok(list(ss.keys()) == ["pillars"], "只有 pillars 一个源：%s" % json.dumps(ss, ensure_ascii=False))
ok((ss.get("pillars") or {}).get("dates", 0) == len(pillar_dates),
   "四支柱期数 = %d（实测 %s）" % (len(pillar_dates), (ss.get("pillars") or {}).get("dates")))

print("\n[I] 引擎自加权（E 支柱）聚合口径 == backtest.payload()")
# E 支柱的逐 (date,pillar,period) 统计必须是 payload() 的同一种口径（同一个
# _window_ret / 同一个候选池基准），否则"引擎自加权"与"历史回测"会给出不同命中率。
# 这里把新引擎 single-pass 聚合结果与 payload() 逐单元 1e-9 内比对，钉死口径。
_bd = PL.self_learn_period_breakdown(con)
ok(len(_bd) > 0, "self_learn_period_breakdown 产出 %d 个 (date,pillar,period) 单元" % len(_bd))
_tot_i = _ok_i = 0
for (_d, _p, _per) in _bd:
    _pd = BT.payload(con, date=_d, pillar=_p)
    if not _pd.get("ok"):
        continue
    _st = _pd["period_stats"].get(str(_per))
    if not _st:
        continue
    _tot_i += 1
    _b = _bd[(_d, _p, _per)]
    _same_n = (_b["n"] == _st["n"])
    _same_hr = ((_b["hit_rate"] is None) == (_st["hit_rate"] is None)) and (
        _b["hit_rate"] is None or abs(_b["hit_rate"] - _st["hit_rate"]) < 1e-9)
    _same_aa = ((_b["avg_alpha"] is None) == (_st["avg_alpha"] is None)) and (
        _b["avg_alpha"] is None or abs(_b["avg_alpha"] - _st["avg_alpha"]) < 1e-9)
    _good = _same_n and _same_hr and _same_aa
    _ok_i += 1 if _good else 0
    if not _good:
        print("      不一致 %s %s t%d 新块(n=%s,hr=%s,aa=%s) payload(n=%s,hr=%s,aa=%s)"
              % (_d, _p, _per, _b["n"], _b["hit_rate"], _b["avg_alpha"],
                 _st["n"], _st["hit_rate"], _st["avg_alpha"]))
ok(_tot_i > 0 and _ok_i == _tot_i,
   "I 引擎自加权聚合 == payload 口径 %d/%d" % (_ok_i, _tot_i))

print("\n" + ("BACKTEST_CALIBER_FAIL (%d)\n" % len(FAILS) + "\n".join(" - " + f for f in FAILS)
             if FAILS else "BACKTEST_CALIBER_PASS"))
con.close()
sys.exit(1 if FAILS else 0)
