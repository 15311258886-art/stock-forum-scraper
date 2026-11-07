# -*- coding: utf-8 -*-
"""准入闸门：估值分位 + 基本面（gate v1，2026-11-03 进哥要求"把没做的全做出来"）。

## 为什么要有这个模块

此前基本面在 `screener_lowbox` 里是**加分项** —— 烂公司只要跌得够久、箱体够规整，
照样能凑到高分上榜。进哥的要求是「从加分项改闸门」：先过闸，再谈分。

## 阈值怎么定的（不是拍脑袋，是先算过杀伤面）

2026-11-02 在当日 693 只在榜票上逐条试算：

    硬否决（剔除）                          软否决（扣分不剔除）
    pb≤0        1 只 (0%)                 roe<-10      49 只 (7%)
    市值<15亿    5 只 (1%)                 负债率>85    22 只 (3%)
    负债率>95    5 只 (1%)                 营收净利双崩 26 只 (4%)
    roe<-30     12 只 (2%)                PE>150       54 只 (8%)
    ─────────────────────                ─────────────────────
    并集 18 只 (3%)，且**榜单前 10 名零命中**   并集 130 只 (19%)

含义：这批"箱体谷底"票里绝大多数是正常公司，只有 3% 是真该排除的。
所以硬否决定得**窄**（只放客观、无争议、可核对的），软否决用来排序而不是删票。

## 一条重要的口径判断（写下来免得以后有人改错）

**不要用 PE 分位当主闸门。** 实测当前全市场 PE_TTM 非正的有 **1723 只（31%）** ——
亏损公司占三成；而"箱体谷底"策略天生聚困境股（周期底部亏损是常态，例如航运、养殖）。
用 PE 分位一票否决会把大量正确标的误杀。真正稳定的估值锚是 **PB**（净资产不像利润那样
能被一次性减值打到负），所以估值闸门只用 PB 分位，PE 只做极端值排除（>150）。

**行业分位覆盖不足。** `plate_members` 只覆盖 3131 只（56%），做同行业 PB 分位会有一半票
没有基准。故先用全市场 PB 分位；等行业覆盖补全再切换（切换条件写在 `PB_PCT_SCOPE`）。

## 契约

`evaluate(con, codes, as_of=None, diag=None)` → `{code: {"hard": [...], "soft": [...], "metrics": {...}}}`

- `hard` 非空 = 应当剔除；`soft` 非空 = 保留但扣分（`SOFT_PENALTY` 每条，封顶 `SOFT_CAP`）
- 数据缺失（无快照 / 无财报）**不算违例**：缺数据不等于公司烂，如实放进 `metrics["missing"]`
- 全部只用当日及以前的数据，无未来函数
- `diag`（可选，dict）会被填上"这一轮闸门到底参没参与"的证据，供调用方如实转述。
  不传就不做任何额外工作，返回结构一字不变。

## 无前视口径（2026-11-03 QA 修订后补，这一段是踩过坑才写的）

QA 实测发现，原来的取数**只按 `symbol IN (...)` 取财报，没有任何报告期约束**。
而 `fundamentals` 表是「**每股一行**」的单快照（实测 5056 行 / 5056 个 symbol，
报告期只有 2025-09-30 ×1、2026-06-30 ×581、2026-09-30 ×4474 三种，没有多期历史）。
后果是：`evaluate(con, codes, as_of="2025-08-04")` 与 `as_of="2026-07-31"` 返回**完全相同**的
hard/soft —— 8 个历史样本日都在用最多 14 个月之后的财报做硬否决。这是前视，必须堵。

**改法（只加约束，不改阈值）**：取财报后按 `report_date <= as_of` 过滤，
被过滤掉的**计数记进 `diag["n_fund_future_dropped"]`**，不静默丢弃。
线上路径（`as_of=None`）实测过滤掉 0 行（最大报告期 2026-09-30 < 最新快照日 2026-11-02），
所以**线上行为不变**；陈旧判定锚点也保持"今天"（见 `_stale_cutoff`）。

**必须承认的残余偏差**：`report_date` 是**报告期**不是**公告日**，2026-06-30 的半年报实际公告
多在 8 月底。所以 `report_date <= as_of` 是**必要条件不是充分条件**，仍有 ≤2 个月的
"公告滞后型前视"。库里没有公告日字段，消不掉，只能标注 —— 谁要拿历史日跑 A/B，
必须把这一点写进结论，不许当成干净的样本外数字。
"""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

# ---- 硬否决门槛（客观、无争议、可核对的才放这里）----
MV_MIN_YI = 15.0          # 总市值下限（亿元）：微盘壳风险
DR_MAX_HARD = 95.0        # 资产负债率上限（%）：接近资不抵债
ROE_MIN_HARD = -30.0      # ROE 下限（%）：深度亏损

# ---- 软否决门槛（扣分不剔除）----
ROE_MIN_SOFT = -10.0
DR_MAX_SOFT = 85.0
REV_YOY_MIN = -30.0       # 营收同比
NP_YOY_MIN = -50.0        # 净利同比（与营收同时击穿才算"双崩"）
PE_MAX_SOFT = 150.0       # PE 极端值（不用于否决，只用于扣分）
PB_PCT_SOFT = 90.0        # PB 全市场分位 > 90% = 估值处于前 10% 贵

# 行业分位何时启用：`plate_members` 对全市场覆盖率 ≥ 85% 时从"全市场分位"切到"同行业分位"。
# 当前实测 3131/5590 = 56%，不够，所以仍用全市场。
PB_PCT_SCOPE = "market"   # "market" | "industry"（切换需先满足上面的覆盖条件）

SOFT_PENALTY = 2.0        # 每条软否决扣多少分
SOFT_CAP = 6.0            # 软否决最多扣多少分（防止一条规则把好票压死）

FUND_STALE_DAYS = 400     # 财报超过这个天数没更新 → 标为陈旧（不算违例，只提示）


def _yi(v) -> float | None:
    try:
        return float(v) / 1.0e8 if v is not None else None
    except Exception:  # noqa: BLE001
        return None


def _num(v):
    try:
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


def pb_percentiles(con, as_of: str | None = None) -> dict:
    """当日全市场 PB 分位 {code: 0~100}（只在 PB>0 的票里排，负净资产不参与分位）。"""
    try:
        if not as_of:
            row = con.execute("SELECT MAX(trade_date) d FROM stock_daily_snapshot").fetchone()
            as_of = (row and row[0]) or None
        if not as_of:
            return {}
        rows = con.execute(
            "SELECT code, pb FROM stock_daily_snapshot WHERE trade_date=? AND pb>0 ORDER BY pb",
            (as_of,)).fetchall()
        n = len(rows)
        if n < 100:
            return {}
        out = {}
        # 并列取值按同分位处理（用首次出现的下标），避免同 PB 被拆到不同分位
        last_pb, last_pct = None, 0.0
        for i, r in enumerate(rows):
            pb = float(r[1])
            if pb != last_pb:
                last_pct = i / (n - 1) * 100.0
                last_pb = pb
            out[str(r[0])] = round(last_pct, 1)
        return out
    except Exception:  # noqa: BLE001
        log.exception("PB 分位计算失败（闸门将跳过估值项）")
        return {}


def evaluate(con, codes, as_of: str | None = None, diag: dict | None = None) -> dict:
    """对一批股票算准入闸门。返回 {code: {"hard": [...], "soft": [...], "metrics": {...}}}。

    `as_of` 为空则取快照表最新交易日。任何一条规则需要的字段缺失时，
    该规则**静默跳过**（不误判）并记进 `metrics["missing"]`。

    `diag` 为空时行为与旧版完全一致；传一个 dict 进来会被填上本轮"闸门参没参与"的证据
    （当日快照行数、放行/被报告期挡掉的财报行数、陈旧锚点）。历史回溯时**必须**看这个，
    否则会把"闸门其实没生效"误读成"闸门放行了"。
    """
    codes = [str(c) for c in dict.fromkeys(codes or []) if c]
    if not codes:
        return {}
    # 记下调用方**显式**传了 as_of 没有：陈旧判定的锚点按这个分叉（见下方 cutoff）。
    # 线上实盘走的是 as_of=None 那条（锚在"今天"，与旧实现逐字一致）。
    as_of_arg = as_of
    out = {c: {"hard": [], "soft": [], "penalty": 0.0, "metrics": {}, "missing": []} for c in codes}

    if not as_of:
        try:
            row = con.execute("SELECT MAX(trade_date) d FROM stock_daily_snapshot").fetchone()
            as_of = (row and row[0]) or None
        except Exception:  # noqa: BLE001
            as_of = None

    CH = 400
    snap = {}
    fund = {}
    n_fund_future = 0          # 被"报告期晚于 as_of"挡掉的财报行数（诊断用，不静默）
    fund_report_dates = {}     # 实际放行的报告期分布 {报告期: 行数}，让调用方能如实转述
    try:
        for i in range(0, len(codes), CH):
            chunk = codes[i:i + CH]
            ph = ",".join("?" * len(chunk))
            for r in con.execute(
                    "SELECT code,pe_ttm,pb,total_mv,close FROM stock_daily_snapshot"
                    " WHERE trade_date=? AND code IN (%s)" % ph, [as_of] + chunk):
                snap[str(r[0])] = {"pe_ttm": r[1], "pb": r[2], "total_mv": r[3], "close": r[4]}
            # 财报：取回报告期，在 Python 里按 `报告期 <= as_of` 过滤。
            # 为什么不在 SQL 里加 WHERE report_date<=?：本项目库里 report_date 允许为空，
            # SQL 的三值逻辑会把 NULL 行一起丢掉（等于"缺日期就不给用"），
            # 而在 Python 里能明确写成"没日期就照旧放行、有日期才比大小"，
            # 顺手还能数出被挡掉多少行 —— 诊断计数比省这一次过滤更重要。
            for r in con.execute(
                    "SELECT symbol,roe,dr,gm,rev_yoy,np_yoy,report_date FROM fundamentals"
                    " WHERE symbol IN (%s)" % ph, chunk):
                rpt = (r[6] or "")[:10]
                if as_of and rpt and rpt > str(as_of)[:10]:
                    n_fund_future += 1
                    continue
                fund[str(r[0])] = {"roe": r[1], "dr": r[2], "gm": r[3],
                                   "rev_yoy": r[4], "np_yoy": r[5], "report_date": r[6]}
                rpt_key = rpt or "（无报告期）"
                fund_report_dates[rpt_key] = fund_report_dates.get(rpt_key, 0) + 1
    except Exception:  # noqa: BLE001
        log.exception("闸门取数失败（本轮按无数据处理，不误剔）")

    # 陈旧锚点：显式传了 as_of（历史回溯）就锚在 as_of，否则锚在今天（线上口径，与旧实现一致）
    cutoff = _stale_cutoff(as_of_arg)

    pb_pct = pb_percentiles(con, as_of) if PB_PCT_SCOPE == "market" else {}

    if diag is not None:
        diag.update({
            "as_of": as_of,
            "as_of_explicit": bool(as_of_arg),
            "n_codes": len(codes),
            "n_snap": len(snap),                       # >0 才说明快照类规则（PB/市值/PE）能生效
            "n_fund": len(fund),                       # >0 才说明财报类规则能生效
            "n_fund_future_dropped": n_fund_future,
            "fund_report_dates": dict(sorted(fund_report_dates.items())),
            "n_pb_pct": len(pb_pct),
            "stale_cutoff": cutoff,
            # 闸门"参与了"的定义：至少有一类规则拿得到数据。两类都空 = 这一轮等于没设闸。
            "effective": bool(snap or fund),
        })

    for c in codes:
        o = out[c]
        s = snap.get(c)
        f = fund.get(c)
        if not s:
            o["missing"].append("快照")
        if not f:
            o["missing"].append("财报")

        # ── 硬否决 ────────────────────────────────────────────────
        if s:
            pb = _num(s["pb"])
            mv = _yi(s["total_mv"])
            o["metrics"]["pb"] = pb
            o["metrics"]["mv_yi"] = round(mv, 1) if mv is not None else None
            if pb is not None and pb <= 0:
                o["hard"].append("净资产为负（PB≤0）")
            if mv is not None and mv < MV_MIN_YI:
                o["hard"].append("市值 %.0f 亿 < %.0f 亿" % (mv, MV_MIN_YI))
        if f:
            roe = _num(f["roe"])
            dr = _num(f["dr"])
            o["metrics"]["roe"] = roe
            o["metrics"]["dr"] = dr
            if dr is not None and dr > DR_MAX_HARD:
                o["hard"].append("资产负债率 %.0f%% > %.0f%%" % (dr, DR_MAX_HARD))
            if roe is not None and roe < ROE_MIN_HARD:
                o["hard"].append("ROE %.0f%% < %.0f%%" % (roe, ROE_MIN_HARD))

        # ── 软否决（只在没被硬否决时算，避免重复扣分）────────────
        if not o["hard"]:
            if f:
                roe = _num(f["roe"])
                dr = _num(f["dr"])
                rev = _num(f["rev_yoy"])
                np_ = _num(f["np_yoy"])
                if roe is not None and roe < ROE_MIN_SOFT:
                    o["soft"].append("ROE %.0f%% < %.0f%%" % (roe, ROE_MIN_SOFT))
                if dr is not None and dr > DR_MAX_SOFT:
                    o["soft"].append("负债率 %.0f%% > %.0f%%" % (dr, DR_MAX_SOFT))
                if rev is not None and np_ is not None and rev < REV_YOY_MIN and np_ < NP_YOY_MIN:
                    o["soft"].append("营收 %.0f%% / 净利 %.0f%% 双降" % (rev, np_))
                rpt = (f.get("report_date") or "")[:10]
                if rpt and rpt < cutoff:
                    o["soft"].append("财报停留 %s（数据陈旧）" % rpt)
            if s:
                pe = _num(s["pe_ttm"])
                if pe is not None and pe > PE_MAX_SOFT:
                    o["soft"].append("PE %.0f > %.0f" % (pe, PE_MAX_SOFT))
                pct = pb_pct.get(c)
                if pct is not None:
                    o["metrics"]["pb_pct"] = pct
                    if pct > PB_PCT_SOFT:
                        o["soft"].append("PB %.1f 处于全市场前 %.0f%%（估值贵）"
                                         % (o["metrics"]["pb"] or 0, 100 - pct))
            o["penalty"] = min(SOFT_CAP, SOFT_PENALTY * len(o["soft"]))
        o["level"] = "hard" if o["hard"] else ("soft" if o["soft"] else None)
    return out


def _stale_cutoff(as_of: str | None = None) -> str:
    """财报"陈旧"的下界日期（YYYY-MM-DD）。按 `FUND_STALE_DAYS` 从锚点日回推。

    锚点：`as_of` 为空 → **今天**（线上实盘口径，与旧实现逐字一致，改这里会让线上软否决集合漂移）；
    `as_of` 给了日期 → 锚在 `as_of`（历史回溯必须站到那一天看，否则 2025 年的样本会用
    2026 年的"陈旧线"，等于把当时的正常财报判成陈旧）。解析不了就当没给。
    """
    import datetime as _dt
    anchor = None
    if as_of:
        try:
            anchor = _dt.date.fromisoformat(str(as_of)[:10])
        except Exception:  # noqa: BLE001
            anchor = None
    if anchor is None:
        anchor = _dt.date.today()
    return (anchor - _dt.timedelta(days=FUND_STALE_DAYS)).strftime("%Y-%m-%d")


def summary(gate: dict) -> dict:
    """给面板用的一句话统计 + 规则命中明细（便于用户核账，不是黑箱）。"""
    n = len(gate)
    hard = [c for c, v in gate.items() if v.get("hard")]
    soft = [c for c, v in gate.items() if v.get("soft") and not v.get("hard")]
    rule_cnt: dict = {}
    for v in gate.values():
        for r in (v.get("hard") or []) + (v.get("soft") or []):
            k = r.split(" ")[0].split("（")[0]
            rule_cnt[k] = rule_cnt.get(k, 0) + 1
    return {
        "n": n, "n_hard": len(hard), "n_soft": len(soft),
        "n_pass": n - len(hard), "rule_cnt": rule_cnt,
        "params": {"mv_min_yi": MV_MIN_YI, "dr_max_hard": DR_MAX_HARD,
                   "roe_min_hard": ROE_MIN_HARD, "roe_min_soft": ROE_MIN_SOFT,
                   "dr_max_soft": DR_MAX_SOFT, "pe_max_soft": PE_MAX_SOFT,
                   "pb_pct_soft": PB_PCT_SOFT, "pb_pct_scope": PB_PCT_SCOPE,
                   "soft_penalty": SOFT_PENALTY, "soft_cap": SOFT_CAP},
    }


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from viewer.ann_feed import DB_PATH
    from viewer import db as _db
    from viewer import universe_filter as UF
    con = _db.get_main_conn(read_only=True)
    try:
        u = UF.evaluation_universe(con)
        g = evaluate(con, u[:2000])
        print(summary(g))
    finally:
        con.close()
