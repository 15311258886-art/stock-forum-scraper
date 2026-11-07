# -*- coding: utf-8 -*-
"""箱体标尺 A/B：static（静态 750 日箱体）vs blend（静态 + 动态布林分位）

## 这是干什么的

2026-11-03 的 D71 给中线波段加了一把**动态**的尺子（布林 20 日 ±2σ 的 %B 分位），
与原来的静态箱体并行，默认 `static` 不生效。本脚本回答一个必须用数字回答的问题：

> 换这把尺，**用户每天看到的那 40 只票，是变好了还是变坏了？**

只有一件事能回答它：**这些票在榜单日之后的真实收益**。所以本脚本严格按下面这条口径算：

- 榜单日 D 的判定，只喂 D 日及以前的 K 线（`_slice_upto`），一根未来 K 线都不许进 `scan_one`；
- 前向收益用 D 日收盘 → D+5 / D+10 / D+20 **交易日**收盘（交易日按 `kbase` 的全市场日历对齐，
  不是按每只票自己的第 N 根 K 线 —— 停牌票的"第 5 根"不是市场的第 5 天，混用会让两组的
  持有期长度不同）；
- 某只票在目标日停牌时，退化到"目标日之前最近一根"，但仍然严格要求该根之后于 D 日；
- 目标日还没有 K 线的（最近几个样本日会遇到），标 NaN 并从该 horizon 的均值里剔除，
  同时把有效样本数打出来 —— 不假装自己算得出来。

## 为什么还要跑多个样本日

单日 Top40 的 T+20 均值是一个四十分样本的横截面，噪声极大。所以默认跑 3 个样本日
（间隔 40 个交易日，覆盖约半年），既给单日明细（验收要求），也给跨日汇总（结论强度）。
汇总口径是「各样本日均值的平均」，不是把所有票混在一起平均 —— 后者会让票多的日子主导结论。

## 纪律

- 两个模式跑的是**同一批 K 线、同一套门槛**：`passed1` / `passed2` 必然相等。
  这不是脚本写错，是设计使然：D71 只改"位置怎么量"，没动任何门槛。
  所以差异只能来自排序 → Top40 组成 → 收益。脚本会把这句话印出来，避免被读成"新尺筛掉了更多票"。
- 准入闸门（`viewer/gate.py`）两个模式都跑一遍，用 `as_of=样本日`，并且**真的会检查它有没有生效**
  （2026-11-03 QA 指出原版这里是一句假承诺：只写了"会明说"，实际从不检查）。
  事实是：`stock_daily_snapshot` 只有 2026-10-27 起的 4 个交易日，`fundamentals` 是
  **每股一行的单快照**（报告期只有 2025-09-30 / 2026-06-30 / 2026-09-30 三种，没有多期历史）。
  所以历史样本日上闸门**很可能拿不到数据** —— 那就在该样本日打一行 `⚠ 闸门未参与` 并给出原因，
  汇总里报「闸门生效 N/M 天」。
  闸门未参与**不影响 static/blend 的配对对比**（两边同样没有闸门），但**绝对收益水平**和
  「Top40 有没有跑赢候选池」这类比较，必须按"无闸门口径"来读 —— 脚本会把每个样本日的
  `Top40 − 候选池` 差值直接算出来打出来，不靠人眼估。

用法：
    python tools/ab_box_mode.py                       # 3 个样本日 × static/blend
    python tools/ab_box_mode.py --dates 1             # 只跑最近一个可评估日
    python tools/ab_box_mode.py --asof 2026-09-30 --dates 1
    python tools/ab_box_mode.py --modes static,blend,dyn
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from viewer import universe_box as ub            # noqa: E402  被测对象本身
from viewer import localfeed as lf               # noqa: E402  交易日历 / 名称表

HORIZONS = (5, 10, 20)          # 前向收益的交易日跨步
SAMPLE_GAP = 40                # 多个样本日之间的间隔（交易日）；40 约等于两个月


# ---------------------------------------------------------------- 日历/切片工具
def _idx_le(bars: list, date: str) -> int:
    """最后一个 `bars[i][0] <= date` 的下标；一个都没有则 -1。

    自己写二分而不是 bisect(key=...) 是为了不依赖 3.10+ 的 key 参数 ——
    这个脚本要能在任何能跑主程序的解释器上跑。
    """
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid][0] <= date:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def _slice_upto(bars: list, asof: str, keep: int) -> list:
    """取截止 asof（含）的最后 keep 根 K 线。**这是"不许前视"的唯一关卡**。

    返回的是**同一批 tuple 的引用**（不复制），所以多个样本日同时在场时内存只多几个列表头。
    """
    j = _idx_le(bars, asof)
    if j < 0:
        return []
    lo = max(0, j + 1 - keep)
    return bars[lo:j + 1]


def _fwd_ret(bars: list, i0: int, target: str) -> float | None:
    """D 日收盘 → target 日收盘的收益。i0 是 D 日在 bars 里的下标。

    停牌处理：目标日没有 K 线就退化到"目标日之前最近一根"，但必须严格晚于 D 日
    （`j <= i0` 直接给 None，宁可少一个样本也不许把 D 日当天当未来用）。
    """
    if i0 < 0 or i0 >= len(bars):
        return None
    j = _idx_le(bars, target)
    if j <= i0:
        return None
    c0, c1 = bars[i0][2], bars[j][2]
    if not c0 or not c1 or c0 <= 0:
        return None
    return c1 / c0 - 1.0


# ---------------------------------------------------------------- 统计工具
def _spearman(x: list, y: list) -> float:
    """秩相关（平均秩处理并列）。用来回答"两把尺排出来的名次到底差多远"。"""
    n = len(x)
    if n < 3:
        return float("nan")

    def _rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = _rank(x), _rank(y)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((v - mx) ** 2 for v in rx) ** 0.5
    dy = sum((v - my) ** 2 for v in ry) ** 0.5
    return num / (dx * dy) if dx and dy else float("nan")


def _mean(vals: list) -> float | None:
    v = [x for x in vals if x is not None]
    return (sum(v) / len(v)) if v else None


def _fmt_pct(v: float | None, width: int = 8) -> str:
    return ("%*s" % (width, "—")) if v is None else ("%+*.2f%%" % (width, v * 100.0))


# ---------------------------------------------------------------- 一次模式运行
def _apply_gate(rows: list, asof: str) -> tuple:
    """照 `universe_box.run()` 的做法过准入闸门，让 Top40 与线上一致。

    为什么必须在这里也做：闸门会扣分（软否决最多 −6）并重排名次。
    不跑闸门的 A/B 算出来的 Top40 跟用户看到的不是同一批票，那样得出的收益结论
    再精确也没用 —— 它评的不是线上那个榜。

    **同时检查闸门到底有没有生效**：`gate.evaluate` 在缺数据时是"静默跳过该条规则"的，
    历史样本日上很可能一条规则都拿不到数据。若不检查，输出会让人以为"闸门放行了"，
    而实际上是"闸门没上班"。判据用 `gate.evaluate(diag=...)` 回填的 `effective`：
    当日快照行数 > 0 或 财报行数 > 0 才算参与了。
    """
    try:
        from viewer import gate as _G
    except Exception:  # noqa: BLE001
        return rows, {"available": False, "gate_effective": False,
                      "reason": "闸门模块导入失败", "diag": {}}
    con = ub._hist_con()                      # noqa: SLF001 项目内同库约定
    diag: dict = {}
    try:
        gm = _G.evaluate(con, [x["code"] for x in rows], as_of=asof, diag=diag)
        sm = _G.summary(gm)
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    kept = []
    for x in rows:
        g = gm.get(x["code"]) or {}
        if g.get("hard"):
            continue
        pen = g.get("penalty") or 0.0
        if pen:
            x = dict(x)                       # 不改 scan_universe 的原始行，避免两次调用互相污染
            x["tech_score"] = x["score"]
            x["score"] = round(max(0.0, x["score"] - pen), 2)
            x["gate_penalty"] = pen
        kept.append(x)
    kept.sort(key=lambda r: -r["score"])
    for i, x in enumerate(kept):
        x["rank"] = i + 1
    sm["available"] = bool(rows)
    sm["n_hard_dropped"] = len(rows) - len(kept)
    sm["gate_effective"] = bool(diag.get("effective"))
    sm["diag"] = diag
    return kept, sm


def _gate_line(info: dict) -> str:
    """把闸门的参与情况压成一行，供每个样本日打印。"""
    d = info.get("diag") or {}
    if info.get("reason"):
        return "⚠ 闸门未参与 —— %s" % info["reason"]
    if info.get("gate_effective"):
        rpts = "、".join("%s×%d" % (k, v) for k, v in (d.get("fund_report_dates") or {}).items())
        return ("闸门生效：当日快照 %d 行、财报 %d 行（放行报告期 %s；被报告期挡掉 %d 行）"
                % (d.get("n_snap", 0), d.get("n_fund", 0), rpts or "—",
                   d.get("n_fund_future_dropped", 0)))
    why = []
    if not d.get("n_snap"):
        why.append("样本日无当日快照行（stock_daily_snapshot 只有 2026-10-27 起 4 天）")
    if not d.get("n_fund"):
        why.append("财报被报告期挡住 %d 行（fundamentals 为每股一行的单快照，无多期历史）"
                   % d.get("n_fund_future_dropped", 0))
    return "⚠ 闸门未参与 —— " + "；".join(why or ["两类数据都为空"])


def _excess(top_fwd: dict, pool_fwd: dict) -> dict:
    """Top 组合相对候选池的超额（百分点）。用来回答"排序到底有没有选股价值"。"""
    out = {}
    for h in HORIZONS:
        k = "T+%d" % h
        a, b = top_fwd.get(k, {}).get("mean"), pool_fwd.get(k, {}).get("mean")
        out[k] = (a - b) * 100.0 if (a is not None and b is not None) else None
    return out



def run_mode(kl: dict, names: dict, mode: str, top: int, min_dwell: float,
             asof: str) -> dict:
    """跑一个模式，返回 {passed1, passed2, top, all, gate, elapsed_ms}。"""
    t0 = time.time()
    res = ub.scan_universe(kl, limit=top, min_dwell=min_dwell, names=names,
                           keep_all=True, box_mode=mode)
    all_rows, gate_info = _apply_gate(res.pop("all_rows", []) or [], asof)
    return {
        "mode": mode,
        "passed1": res["passed1"],
        "passed2": len(all_rows),
        "top": all_rows[:top],
        "all": all_rows,
        "gate": gate_info,
        "params": res["params"],
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


# ---------------------------------------------------------------- 收益评估
def eval_top(top_rows: list, bars_all: dict, cal: list, asof: str) -> dict:
    """算一组票的 T+5/10/20 前向收益均值 + 中位数 + 有效样本数。"""
    i_asof = cal.index(asof)
    out = {}
    for h in HORIZONS:
        target = cal[i_asof + h] if i_asof + h < len(cal) else None
        rets = []
        if target:
            for r in top_rows:
                bars = bars_all.get(r["code"]) or []
                i0 = _idx_le(bars, asof)
                # 严格：D 日当天必须有这根 K 线，否则基准价是"更早的最后一根"，
                # 那只票的持有期起点就不是 D 日了，混在一起会污染均值。
                if i0 < 0 or bars[i0][0] != asof:
                    continue
                v = _fwd_ret(bars, i0, target)
                if v is not None:
                    rets.append(v)
        out["T+%d" % h] = {
            "target": target,
            "mean": _mean(rets),
            "median": statistics.median(rets) if rets else None,
            "n": len(rets),
            "hit": (sum(1 for x in rets if x > 0) / len(rets)) if rets else None,
        }
    return out


# ---------------------------------------------------------------- 自检
def self_check(kl: dict, sample: int = 40) -> list:
    """两条自检：① 静态位置分与 D65 公式完全一致；② 前缀和近似与 pstdev 等价。

    自检存在的理由：这次改动动了 `score_one` 里唯一那行位置分，而"默认不变"是硬纪律。
    与其事后靠肉眼看榜单，不如让脚本自己证明"static 模式算出来的数与老公式逐位相等"。
    """
    lines = []
    codes = [c for c in list(kl.keys())[:4000] if len(kl[c]) >= ub.MIN_BARS][:sample]
    bad_pos = bad_parts = bad_boll = 0
    worst = 0.0
    for c in codes:
        m = ub.scan_one(kl[c])
        if not m:
            continue
        sc = ub.score_one(m, box_mode="static")
        want = max(0.0, min(1.0, 1.0 - m["pos"] / ub.DWELL_BOTTOM_RATIO)) * 100.0
        if abs(sc["parts"]["pos"] - round(want, 1)) > 1e-9:
            bad_pos += 1
        # static 下 parts 的键必须与 D65 一致（多一个键都会让前端/落盘 json 变化）
        if set(sc["parts"].keys()) != {"pos", "dwell", "regular", "touch"}:
            bad_parts += 1
        # ② 用 pstdev 逐日重算 250 日 %B 序列，跟 scan_one 里的前缀和版本比分位差。
        #    窗口必须**完全对齐**：250 个 %B 值 = 最后 250 个交易日，
        #    每个值要用它自己前 19 根预热 → 一共取最后 250+19 = 269 根收盘价。
        #    （第一版这里写成"最后 770 根"，序列变成 751 个值、把两年前的行情也算进分位，
        #      差异直接冲到 17 个百分点 —— 是自检对齐错，不是算法错。）
        closes = [float(b[2]) for b in kl[c][-ub.BOX_WIN:] if b[2]]
        closes = closes[-(ub.BOLL_RANK_WIN + ub.BOLL_WIN - 1):]
        series = []
        for i in range(ub.BOLL_WIN - 1, len(closes)):
            w = closes[i - ub.BOLL_WIN + 1:i + 1]
            mid = sum(w) / float(ub.BOLL_WIN)
            sd = statistics.pstdev(w)
            band = 2.0 * ub.BOLL_K * sd
            if band > 0:
                series.append((closes[i] - (mid - ub.BOLL_K * sd)) / band)
        if series and len(series) >= ub.BOLL_RANK_WIN // 2:
            ref = ub._pct_rank(series, series[-1])      # noqa: SLF001 自检专用
            got = m.get("boll_pctb_rank")
            if got is not None:
                worst = max(worst, abs(ref - got))
    lines.append("  ① static 位置分与 D65 公式不一致的票：%d 只（应为 0）" % bad_pos)
    lines.append("  ② static 模式下 parts 键集与 D65 不一致的票：%d 只（应为 0）" % bad_parts)
    lines.append("  ③ 前缀和近似 vs pstdev 逐日重算：布林 %%B 分位最大差 %.2f 个百分点"
                 "（≤1.0 视为等价；差异只来自第 13 位小数）" % worst)
    if worst > 1.0:
        bad_boll += 1
    lines.append("  → 自检%s" % ("通过" if (bad_pos == 0 and bad_parts == 0 and bad_boll == 0)
                                else "**未通过**，先查再信下面的收益数字"))
    return lines


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", type=int, default=3, help="样本日个数（默认 3，间隔 40 个交易日）")
    ap.add_argument("--asof", default="", help="指定单个样本日（YYYY-MM-DD），会覆盖 --dates")
    ap.add_argument("--modes", default="static,blend", help="要对比的模式，逗号分隔")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--min-dwell", type=float, default=0.15)
    ap.add_argument("--selfcheck", action="store_true", help="只跑自检，不做 A/B")
    a = ap.parse_args()

    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    for m in modes:
        if m not in ub.BOX_MODES:
            print("模式 %r 不合法（合法值 %s）" % (m, " / ".join(ub.BOX_MODES)))
            return 2

    cal = lf._kbase().distinct_dates()          # noqa: SLF001 与主程序同一交易日历
    if not cal:
        print("取不到交易日历（kbase 不可用）")
        return 1

    # ---- 选样本日：必须"前面够 750 根、后面够 20 根"，否则算不出箱体或算不出收益 ----
    max_h = max(HORIZONS)
    if a.asof:
        if a.asof not in cal:
            print("%s 不是 kbase 里的交易日" % a.asof)
            return 2
        asofs = [a.asof]
    else:
        n = max(1, int(a.dates))
        asofs = []
        for k in range(n):
            back = max_h + 1 + k * SAMPLE_GAP
            if back >= len(cal):
                break
            asofs.append(cal[-back])
        asofs.sort()
    if not asofs:
        print("没有可评估的样本日")
        return 1

    # ---- 取数：一次性取到"最早样本日往前 810 根 + 往后 20 根"，各样本日只做内存切片 ----
    codes = None
    try:
        from viewer import universe_filter as _uf
        codes = _uf.evaluation_universe()
    except Exception:  # noqa: BLE001
        codes = None
    fwd_need = len(cal) - 1 - cal.index(asofs[0])
    keep = ub.LOAD_KEEP + fwd_need
    t0 = time.time()
    bars_all = ub.load_bars(codes, keep=keep)
    names = {}
    try:
        names = lf.pool(with_names=True)
    except Exception:  # noqa: BLE001
        names = {}
    print("取数：%d 只 × ≤%d 根，耗时 %.1fs" % (len(bars_all), keep, time.time() - t0))
    if not bars_all:
        print("没有取到 K 线（kbase 不可用？）")
        return 1

    if a.selfcheck:
        print("\n" + "=" * 92)
        print("自检（static 模式必须与 D65 逐位一致）")
        print("=" * 92)
        for ln in self_check(bars_all):
            print(ln)
        return 0

    print("\n" + "=" * 92)
    print("箱体标尺 A/B · 模式 %s · Top%d · 样本日 %s"
          % (" vs ".join(modes), a.top, "、".join(asofs)))
    print("=" * 92)
    print("纪律：两个模式吃同一批 K 线、同一套门槛（驻留下限 %.2f / 触及≥%d），"
          % (a.min_dwell, ub.TOUCH_MIN))
    print("      所以 passed1 / passed2 **必然相等** —— 差异只能来自排序，不是筛掉了更多票。")

    # 累积器：{mode: {"T+5": [各样本日均值...]}}
    pooled = {m: {"T+%d" % h: [] for h in HORIZONS} for m in modes}
    pooled_pool = {m: {"T+%d" % h: [] for h in HORIZONS} for m in modes}
    pooled_excess = {m: {"T+%d" % h: [] for h in HORIZONS} for m in modes}
    detail = []
    gate_effective_days = 0
    gate_days = 0

    for asof in asofs:
        kl = {}
        for c, bars in bars_all.items():
            s = _slice_upto(bars, asof, ub.LOAD_KEEP)
            if len(s) >= ub.MIN_BARS:
                kl[c] = s
        print("\n" + "-" * 92)
        print("样本日 %s ｜ 可用 K 线 ≥%d 根的票 %d 只" % (asof, ub.MIN_BARS, len(kl)))
        print("-" * 92)
        if len(kl) < 100:
            print("  可评估票数太少，跳过本样本日")
            continue

        got = {}
        for mode in modes:
            got[mode] = run_mode(kl, names, mode, a.top, a.min_dwell, asof)
            r = got[mode]
            print("  [%-5s] passed1 %4d · passed2 %4d · 闸门剔 %3d 只 · 耗时 %dms"
                  % (mode, r["passed1"], r["passed2"],
                     r["gate"].get("n_hard_dropped", 0), r["elapsed_ms"]))
            ev = eval_top(r["top"], bars_all, cal, asof)
            r["fwd"] = ev
            r["pool_fwd"] = eval_top(r["all"], bars_all, cal, asof)
            ex = _excess(ev, r["pool_fwd"])
            r["excess"] = ex
            for h in HORIZONS:
                k = "T+%d" % h
                if ev[k]["mean"] is not None:
                    pooled[mode][k].append(ev[k]["mean"])
                if r["pool_fwd"][k]["mean"] is not None:
                    pooled_pool[mode][k].append(r["pool_fwd"][k]["mean"])
                if ex[k] is not None:
                    pooled_excess[mode][k].append(ex[k])
            print("           Top%d 前向均值：T+5 %s  T+10 %s  T+20 %s"
                  % (a.top, _fmt_pct(ev["T+5"]["mean"]), _fmt_pct(ev["T+10"]["mean"]),
                     _fmt_pct(ev["T+20"]["mean"])))
            print("           全候选池（passed2 %d 只）：T+5 %s  T+10 %s  T+20 %s"
                  % (r["passed2"], _fmt_pct(r["pool_fwd"]["T+5"]["mean"]),
                     _fmt_pct(r["pool_fwd"]["T+10"]["mean"]),
                     _fmt_pct(r["pool_fwd"]["T+20"]["mean"])))
            print("           Top%d − 候选池：T+5 %+.2fpp  T+10 %+.2fpp  T+20 %+.2fpp"
                  % (a.top, ex["T+5"] or 0.0, ex["T+10"] or 0.0, ex["T+20"] or 0.0))

        # ---- 闸门到底有没有上班（两模式输入相同 → 结论必然相同；不同就说明有意外，要报）----
        gate_days += 1
        g0 = got[modes[0]]["gate"]
        if g0.get("gate_effective"):
            gate_effective_days += 1
        print("  闸门：%s" % _gate_line(g0))
        for mode in modes[1:]:
            if bool(got[mode]["gate"].get("gate_effective")) != bool(g0.get("gate_effective")):
                print("        ⚠ 模式间闸门参与状态不一致（%s）：%s"
                      % (mode, _gate_line(got[mode]["gate"])))

        # ---- 名单重合度（第一个被对比的模式 vs 其余）----
        base = modes[0]
        base_codes = [x["code"] for x in got[base]["top"]]
        for mode in modes[1:]:
            other = [x["code"] for x in got[mode]["top"]]
            inter = len(set(base_codes) & set(other))
            union = len(set(base_codes) | set(other))
            print("  名单重合 [%s vs %s]：交集 %d / 并集 %d / Jaccard %.3f"
                  % (base, mode, inter, union, (inter / union) if union else 0.0))
            # 全候选池上的名次相关：衡量"尺子换了多少"（不只看头部 40 只）
            common = {x["code"]: x["score"] for x in got[base]["all"]}
            s2 = {x["code"]: x["score"] for x in got[mode]["all"]}
            cc = [c for c in common if c in s2]
            if len(cc) >= 10:
                print("           全池分数秩相关 ρ = %+.3f（%d 只共同候选；=1 表示两把尺完全同序）"
                      % (_spearman([common[c] for c in cc], [s2[c] for c in cc]), len(cc)))
            detail.append({"asof": asof, "base": base, "other": mode, "inter": inter,
                           "union": union, "jaccard": (inter / union) if union else 0.0,
                           "fwd": {base: got[base].get("fwd"), mode: got[mode].get("fwd")}})

    # ---- 跨样本日汇总 ----
    print("\n" + "=" * 92)
    print("跨 %d 个样本日汇总（口径：各样本日 Top%d 均值的算术平均，不是把票混在一起算）"
          % (len(pooled[modes[0]]["T+5"]) or len(asofs), a.top))
    print("=" * 92)
    print("  %-8s %14s %14s %14s" % ("模式", "T+5", "T+10", "T+20"))
    for mode in modes:
        cells = []
        for h in HORIZONS:
            v = _mean(pooled[mode]["T+%d" % h])
            cells.append(_fmt_pct(v, 14))
        print("  %-8s %s %s %s" % (mode, cells[0], cells[1], cells[2]))
    print("  %-8s %14s %14s %14s" % ("(参考)全池", "", "", ""))
    for mode in modes:
        cells = [_fmt_pct(_mean(pooled_pool[mode]["T+%d" % h]), 14) for h in HORIZONS]
        print("    %-6s %s %s %s" % (mode, cells[0], cells[1], cells[2]))
    print("  注：全池均值是同一批候选（passed2）的等权收益，用来判断『Top40 有没有选出更好的』，"
          "而不是判断『市场涨没涨』。")

    # ---- Top40 相对候选池的超额：直接回答"排序到底有没有选股价值" ----
    print("\n  合并口径的超额（Top40 − 同模式候选池，百分点）：")
    print("  %-8s %12s %12s %12s" % ("模式", "T+5", "T+10", "T+20"))
    for mode in modes:
        cells = []
        for h in HORIZONS:
            v = _mean(pooled_excess[mode]["T+%d" % h])
            cells.append("%12s" % ("—" if v is None else "%+.2f" % v))
        print("  %-8s %s %s %s" % (mode, cells[0], cells[1], cells[2]))
    print("  → 正数 = 榜单选出的 40 只比同池子等权更好；≤0 = 排序在这一档上没有贡献。")

    # ---- 闸门参与情况（这句话决定上面所有绝对数字该怎么读）----
    print()
    if gate_days and gate_effective_days == gate_days:
        print("  闸门：%d/%d 个样本日生效 —— 上面的绝对收益可读作『含闸门』口径。"
              % (gate_effective_days, gate_days))
    elif gate_effective_days == 0:
        print("  ⚠ 闸门：0/%d 个样本日生效 —— **上面的绝对收益是『无闸门』口径**。" % gate_days)
        print("     原因：`stock_daily_snapshot` 只有 2026-10-27 起的 4 个交易日，"
              "`fundamentals` 是每股一行的单快照（无多期历史），")
        print("     历史样本日拿不到任何『as_of 当天可见』的估值/财报表 → 闸门的两类规则都没数据。")
        print("     这**不影响** static/blend 的配对差（两边同样没闸门），"
              "但绝对水平不能当成线上含闸门的成绩。")
    else:
        print("  ⚠ 闸门：仅 %d/%d 个样本日生效 —— 上面是**混合口径**，逐日见每个样本日的闸门行。"
              % (gate_effective_days, gate_days))
        print("     配对差仍成立（两模式同日同口径），但跨日汇总的绝对值要谨慎读。")

    # ---- 逐样本日配对差（同一交易日、同一批候选、只换尺：这是最干净的配对比较）----
    for b_mode in modes[1:]:
        a_mode = modes[0]
        rows_b = [r for r in detail if r["base"] == a_mode and r["other"] == b_mode]
        if not rows_b:
            continue
        print("\n  逐样本日配对差（%s − %s，单位：百分点）：" % (b_mode, a_mode))
        print("  %-12s %10s %10s %10s" % ("样本日", "T+5", "T+10", "T+20"))
        sign = {"T+%d" % h: 0 for h in HORIZONS}
        for row in rows_b:
            txt = []
            for h in HORIZONS:
                k = "T+%d" % h
                va, vb = row.get("fwd", {}).get(a_mode), row.get("fwd", {}).get(b_mode)
                if va and vb and va[k]["mean"] is not None and vb[k]["mean"] is not None:
                    d = (vb[k]["mean"] - va[k]["mean"]) * 100.0
                    if d > 0:
                        sign[k] += 1
                    txt.append("%+10.2f" % d)
                else:
                    txt.append("%10s" % "—")
            print("  %-12s %s" % (row["asof"], " ".join(txt)))
        nd = len(rows_b)
        print("  %s 更优的样本日数：T+5 %d/%d、T+10 %d/%d、T+20 %d/%d"
              % (b_mode, sign["T+5"], nd, sign["T+10"], nd, sign["T+20"], nd))

    # ---- 差异票明细（拿最后一个样本日，验收要求 5+5）----
    if len(modes) >= 2 and detail:
        asof = detail[-1]["asof"]
        kl = {}
        for c, bars in bars_all.items():
            s = _slice_upto(bars, asof, ub.LOAD_KEEP)
            if len(s) >= ub.MIN_BARS:
                kl[c] = s
        got = {m: run_mode(kl, names, m, a.top, a.min_dwell, asof) for m in modes[:2]}
        m1, m2 = modes[0], modes[1]
        s1 = {x["code"]: x for x in got[m1]["top"]}
        s2 = {x["code"]: x for x in got[m2]["top"]}
        only1 = [x for x in got[m1]["top"] if x["code"] not in s2][:5]
        only2 = [x for x in got[m2]["top"] if x["code"] not in s1][:5]
        sc1 = {x["code"]: x for x in got[m1]["all"]}
        sc2 = {x["code"]: x for x in got[m2]["all"]}
        print("\n" + "=" * 92)
        print("差异票明细 · 样本日 %s（各列 5 只）" % asof)
        print("=" * 92)
        print("  「%s 在榜、%s 不在榜」：" % (m1, m2))
        for x in only1:
            o = sc2.get(x["code"]) or {}
            print("    %s %-6s %s %.2f 分 ｜ %s %.2f 分（排名 %s）｜ 静态pos %.0f%% "
                  "｜ 布林%%B %s · 分位 %s"
                  % (x["code"], (x.get("name") or "")[:6], m1, x["score"], m2,
                     o.get("score", float("nan")), o.get("rank", "—"),
                     (x.get("pos") or 0) * 100, x.get("boll_pctb"), x.get("boll_pctb_rank")))
        print("  「%s 在榜、%s 不在榜」：" % (m2, m1))
        for x in only2:
            o = sc1.get(x["code"]) or {}
            print("    %s %-6s %s %.2f 分 ｜ %s %.2f 分（排名 %s）｜ 静态pos %.0f%% "
                  "｜ 布林%%B %s · 分位 %s"
                  % (x["code"], (x.get("name") or "")[:6], m2, x["score"], m1,
                     o.get("score", float("nan")), o.get("rank", "—"),
                     (x.get("pos") or 0) * 100, x.get("boll_pctb"), x.get("boll_pctb_rank")))

    # ---- 自检 ----
    print("\n" + "=" * 92)
    print("自检")
    print("=" * 92)
    for ln in self_check(bars_all):
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
