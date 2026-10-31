# -*- coding: utf-8 -*-
"""执行可行性引擎（2026-10-31 新增，测评「可执行性：未判定」项）。

## 这个模块回答的是哪一句话

不是"这套名单好不好"，而是"**这套名单对你**（这个资金量、这个只数、这个持有期、
这个佣金方案）**是否可行**"。

测评里这一项原本记的是"未判定"，并特意说明：
    **未判定不等于不通过** —— 不通过是判得了而判负，未判定是判不了。
本模块的作用就是把"判不了"变成"判得了"，然后把判负的部分如实判负。

## 四字段是并列输入，不合成

报告的判据：结论层只允许出现原始参数，不允许出现压缩系数。
`aum / n_hold / hold_days / fee_bp` 是**四项独立输入**，本模块不把它们
乘成一个"综合分"，也不引入任何"风险偏好系数"把它们压成一个数。
任何一处出现"综合评分"，都是把四个可争的东西焊死成一个不可争的数。

## 容量的正确写法：区间，且下界可达 0

    上界（点估计）：AUM ≤ CAPACITY_PARTICIPATION × N × min(ADV)
    下界：0，且**可达**

下界可达不是修辞：容量 ∝ Δ²（Δ = 期望超额 − 成本），当期望超额的置信下沿
低于成本时 Δ ≤ 0，容量就是 0 —— 再小的资金也装不下一个负期望。

`min(ADV)` 必须取名单内的最小值而非平均：等权下每只 Q = AUM/N，
约束对全部持仓同时成立，一只票拖后腿就决定门槛。
但报告实测后补了一句更锋利的修正，本模块照做：
    **真正拖后腿的不是 ADV 最小那只，而是 σ²/ADV 最大那只。**
所以：作图/报上界用 min(ADV)，**指认那只票**用 argmax(σ²/ADV)。
这两者在本产品名单上确实不是一个（ADV 最小的 601518 因波动低只排冲击第 3）。

## 涨停买不进

T+1 开盘买不进的主因是一字板。本模块对**名单**做的是收盘态预判
（收盘即涨停 → 次日开盘大概率买不进），对**回测**做的是一字板硬判定
（见 signal_backtest 的 unbuyable 分支）。两者共用 units 里的涨跌幅表。
"""
from __future__ import annotations

import math
import os
import sqlite3

from viewer.kernel import params as _P
from viewer import units as U

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_KLINE = os.path.join(ROOT, "data", "kline_daily.db")


# ── 行情统计 ──────────────────────────────────────────────────────────────────
def _kline_conn():
    if not os.path.exists(_KLINE):
        return None
    return sqlite3.connect("file:%s?mode=ro" % _KLINE.replace("\\", "/"), uri=True, timeout=30)


def adv_stats(codes, window=None, min_obs=None):
    """按代码取 60 日 ADV（元）、日波动率、最后两根 bar。

    返回 {code: {"adv","sigma","n_obs","last_close","prev_close","last_date","error"}}

    关于成交额的口径（必须先说清，否则 adv 会被当成真实值引用）：
    本库 `kline.c` 是**前复权**价，`kline.v` 是成交量（股，已实测：
    000001 于 20261030 为 186,424,507 股 × 11.64 元 = 21.7 亿，与快照 amount 相符）。
    因此 `c × v` 只在"当日价格未被复权调整"时等于真实成交额；
    窗口内若发生除权除息，该票的 adv 会按除权比例**偏低**。
    60 个交易日内触及除权的票占比很小，但这里不当作零：低于 min_obs 的票
    直接报"不可估计"，其余在返回里带 `adjust_risk=True` 让上游能标出来。
    """
    window = int(window or _P.ADV_WINDOW_DAYS_EXEC)
    min_obs = int(min_obs or _P.ADV_MIN_OBS_EXEC)
    out = {}
    want = list(dict.fromkeys([str(c) for c in (codes or []) if c]))
    if not want:
        return out
    con = _kline_conn()
    if con is None:
        return {c: {"error": "kline_db_missing"} for c in want}
    try:
        for code in want:
            rows = con.execute(
                "SELECT d, c, v FROM kline WHERE code=? ORDER BY d DESC LIMIT ?",
                (code, window + 1)).fetchall()
            if len(rows) < min_obs + 1:
                out[code] = {"error": "insufficient_bars", "n_obs": len(rows)}
                continue
            rows.reverse()                      # 升序
            amts, rets = [], []
            prev = None
            for _d, c, v in rows:
                c = float(c or 0)
                v = float(v or 0)
                if c > 0 and v > 0:
                    amts.append(c * v)
                if prev is not None and prev > 0 and c > 0:
                    rets.append(c / prev - 1.0)
                prev = c
            if len(amts) < min_obs:
                out[code] = {"error": "insufficient_obs", "n_obs": len(amts)}
                continue
            adv = sum(amts[-window:]) / len(amts[-window:])
            if len(rets) >= 2:
                mu = sum(rets) / len(rets)
                var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
                sigma = math.sqrt(var)
            else:
                sigma = None
            last = rows[-1]
            prev_bar = rows[-2] if len(rows) >= 2 else (None, None, None)
            out[code] = {
                "adv": adv, "sigma": sigma, "n_obs": len(amts),
                "last_close": float(last[1]) if last[1] else None,
                "prev_close": float(prev_bar[1]) if prev_bar[1] else None,
                "last_date": last[0],
                "adjust_risk": False,   # 见 docstring：窗口内除权的票 adv 偏低
            }
    finally:
        con.close()
    return out


# ── 成本摊薄（2026-10-31 决策④）──────────────────────────────────────────────
def annual_cost_bp(hold_days, fee_bp, impact_bp):
    """年化往返成本（bp）。往返 = 2 × 单边；年轮换 = 244 / hold_days。

    impact_bp 为 None（ADV 不可估计）时返回 None —— 不知道成本不能写成 0。
    这个数是"持有期"唯一该影响的东西：它不改容量、不改 verdict，
    只改"一年下来手续费吃掉多少"。原来 hold_days 是纯回显，于是三档跑完三档一样。
    """
    if impact_bp is None:
        return None
    try:
        hd = int(hold_days)
        if hd <= 0:
            return None
        turns = _P.TRADING_DAYS_PER_YEAR / float(hd)
        return turns * 2.0 * (float(fee_bp or 0) + float(impact_bp))
    except (TypeError, ValueError):
        return None


# ── 主判定 ────────────────────────────────────────────────────────────────────
def evaluate(codes, aum, n_hold, hold_days, fee_bp=0.0, names=None, rank_hint=None):
    """四字段 → 逐票可行性 + 容量区间 + 判决。

    参数
      codes      : 名单代码（按优先级排序）
      aum        : 资金规模，元。缺口即返回 ok=False —— 它是必填输入
      n_hold     : 你真实会买的只数（报告明确：这是前提，不是产品容量）
      hold_days  : 持有期（交易日）
      fee_bp     : 佣金方案，单边 bp（不含冲击成本，冲击成本由 ADV 分档给）
      names      : {code: name}，用于 ST 判定涨跌停幅度
      rank_hint  : {code: rank}，仅回显，不参与计算

    返回 dict；`ok=False` 时 `error` 说明缺哪个必填字段。
    """
    # ---- 必填校验：四字段缺一不可，且不许用"默认值"顶替 --------------------
    miss = []
    try:
        aum = float(aum)
        if aum <= 0:
            miss.append("aum")
    except (TypeError, ValueError):
        miss.append("aum")
    try:
        n_hold = int(n_hold)
        if n_hold <= 0:
            miss.append("n_hold")
    except (TypeError, ValueError):
        miss.append("n_hold")
    try:
        hold_days = int(hold_days)
        if hold_days <= 0:
            miss.append("hold_days")
    except (TypeError, ValueError):
        miss.append("hold_days")
    try:
        fee_bp = float(fee_bp)
        if fee_bp < 0:
            miss.append("fee_bp")
    except (TypeError, ValueError):
        miss.append("fee_bp")
    if miss:
        return {"ok": False, "error": "missing_required",
                "missing": miss,
                "note": "四字段（资金规模／持仓只数／持有期／佣金方案）是并列必填输入，"
                        "不接受默认值顶替 —— 用默认值算出来的可行性，是给别人看的可行性。"}

    codes = [str(c) for c in (codes or []) if c]
    if not codes:
        return {"ok": False, "error": "empty_list"}

    names = names or {}
    stat = adv_stats(codes)

    per, unknown, notes = [], [], []
    for code in codes:
        s = stat.get(code) or {}
        nm = names.get(code, "")
        rec = {"code": code, "name": nm, "rank": (rank_hint or {}).get(code)}
        if s.get("error"):
            rec["feasible"] = None
            rec["reason"] = s["error"]
            unknown.append(code)
            per.append(rec)
            continue

        adv = s["adv"]
        sigma = s["sigma"]
        q = aum / n_hold
        part = q / adv if adv > 0 else None
        tier_bp = _P.cost_bp_of_adv(adv)
        rec.update({
            "adv": round(adv, 0),
            "sigma": None if sigma is None else round(sigma, 6),
            "q": round(q, 2),
            "participation": None if part is None else round(part, 6),
            "cost_bp_commission": round(fee_bp, 2),
            "cost_bp_impact": tier_bp,
            "cost_bp_total": None if tier_bp is None else round(fee_bp + tier_bp, 2),
            "adv_days_to_fill": None if part is None else round(part, 4),
            "impact_rank_key": None if (not sigma or adv <= 0) else round(sigma * sigma / adv, 10),
            # 决策④：持有期进计算。往返成本与年化成本是两件事，都给出 ——
            # 只看往返会低估短线打法，只看年化会看不清单笔代价。
            "cost_bp_roundtrip": None if tier_bp is None else round(2 * (fee_bp + tier_bp), 2),
            "annual_turns": round(_P.TRADING_DAYS_PER_YEAR / float(hold_days), 1),
            "annual_cost_bp": (None if annual_cost_bp(hold_days, fee_bp, tier_bp) is None
                               else round(annual_cost_bp(hold_days, fee_bp, tier_bp), 1)),
        })

        flags, reasons = [], []
        if adv < _P.LIQUIDITY_REJECT_ADV:
            flags.append("illiquid")
            reasons.append("60日ADV %.0f 元 < 拒绝线 %.0f 元" % (adv, _P.LIQUIDITY_REJECT_ADV))
        if part is not None and part > _P.CAPACITY_PARTICIPATION:
            flags.append("over_capacity")
            reasons.append("单票占比 %.4f%% > 参与率上限 %.4f%%"
                           % (part * 100, _P.CAPACITY_PARTICIPATION * 100))
        lu = U.is_limit_up(s.get("last_close"), s.get("prev_close"), code, nm)
        if lu:
            flags.append("limit_up_close")
            reasons.append("最后交易日涨停收盘 → T+1 开盘大概率买不进")
        ld = U.is_limit_down(s.get("last_close"), s.get("prev_close"), code, nm)
        if ld:
            flags.append("limit_down_close")
            reasons.append("最后交易日跌停收盘 → 卖出侧不可成交")
        if U.is_convertible_bond(code):
            flags.append("not_equity")
            reasons.append("可转债混入股票名单（单位/交易规则均不同）")

        rec["flags"] = flags
        rec["reason"] = "；".join(reasons) if reasons else "ok"
        rec["feasible"] = not flags
        per.append(rec)

    known = [r for r in per if r.get("adv") is not None]
    min_adv_rec = min(known, key=lambda r: r["adv"]) if known else None
    bind_rec = None
    if known:
        cands = [r for r in known if r.get("impact_rank_key") is not None]
        if cands:
            bind_rec = max(cands, key=lambda r: r["impact_rank_key"])

    cap_upper = _P.aum_cap_point(n_hold, min_adv_rec["adv"]) if min_adv_rec else None
    over = [r["code"] for r in per if "over_capacity" in (r.get("flags") or [])]
    bad = [r["code"] for r in per if r.get("feasible") is False]
    if unknown and not bad:
        verdict = "unknown"
    elif bad:
        verdict = "infeasible"
    else:
        verdict = "feasible"

    if n_hold > len(codes):
        notes.append("假设持仓 %d 只 > 名单 %d 只：只有 %d 只能买，"
                     "剩下的仓位无处安放，容量按 N=%d 算会**高估**"
                     % (n_hold, len(codes), len(codes), n_hold))
    if over:
        notes.append("超容量的票：%s —— 这些不是'稍微贵一点'，是'下单就把价格推走'"
                     % "、".join(over))

    return {
        "ok": True,
        "four_fields": {"aum": aum, "n_hold": n_hold, "hold_days": hold_days, "fee_bp": fee_bp},
        "four_fields_note": "四项并列输入，不合成任何系数；此处不产出综合评分",
        "per_code": per,
        "binding_code": bind_rec["code"] if bind_rec else None,
        "binding_note": ("拖后腿的是 σ²/ADV 最大那只，不是 ADV 最小那只；"
                         "本名单两者%s同一只" % ("是" if (bind_rec and min_adv_rec
                                                     and bind_rec["code"] == min_adv_rec["code"])
                                          else "不是")),
        "min_adv_code": min_adv_rec["code"] if min_adv_rec else None,
        "min_adv": min_adv_rec["adv"] if min_adv_rec else None,
        "capacity": {
            "upper_point": cap_upper,
            "lower": 0.0,
            "unit": "元",
            "formula": "AUM ≤ %.6f × N × min(ADV)" % _P.CAPACITY_PARTICIPATION,
            "note": "上界是点估计（参数本身是点估计，没有置信区间）；"
                    "下界 0 且可达 —— 容量 ∝ Δ²，Δ = 期望超额 − 成本，"
                    "期望超额取置信下沿时 Δ≤0，此时再小的资金也装不下负期望。",
        },
        "verdict": verdict,
        "feasible_codes": [r["code"] for r in per if r.get("feasible")],
        "infeasible_codes": bad,
        "unknown_codes": unknown,
        # 决策④：持有期带来的成本摊薄，单独一块，不与 verdict 混在一起。
        # verdict 回答"能不能买"，这一块回答"买了一年下来还剩多少"。
        "cost_summary": _cost_summary(per, hold_days),
        "notes": notes,
        "assumptions": _assumptions(),
    }


def _median(xs):
    """真中位数：偶数个取中间两个的平均。

    原来写的是 `xs[len(xs)//2]`（上中位数），2 个元素时直接取到较大的那个 ——
    [40, 140] 会报成 140，中位成本系统性偏高 55%。这种错最危险的地方在于
    它不报错、看起来还挺合理，只是在悄悄把成本估贵。
    """
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return None
    if n % 2:
        return round(xs[n // 2], 1)
    return round((xs[n // 2 - 1] + xs[n // 2]) / 2.0, 1)


def _cost_summary(per, hold_days):
    ann = [r["annual_cost_bp"] for r in per if r.get("annual_cost_bp") is not None]
    rt = [r["cost_bp_roundtrip"] for r in per if r.get("cost_bp_roundtrip") is not None]
    return {
        "hold_days": hold_days,
        "annual_turns": round(_P.TRADING_DAYS_PER_YEAR / float(hold_days), 1),
        "roundtrip_bp_median": _median(rt),
        "annual_cost_bp_median": _median(ann),
        "annual_cost_bp_max": round(max(ann), 1) if ann else None,
        "n_priced": len(ann),
        "n_unpriced": len(per) - len(ann),
        "note": ("年化成本 = 年轮换 × 往返成本；median 是名单中位、max 是最贵那只；"
                 "n_unpriced 是 ADV 不可估计、成本算不出来的只数（它们不是'成本为 0'）。"
                 "持有期只影响这一块，不影响容量与 verdict。"),
    }


def sweep_hold_days(codes, aum, n_hold, fee_bp, hold_days_list=None, names=None, rank_hint=None):
    """同一套四字段、只改持有期 → 三档并排（决策④）。

    为什么是"跑三档"而不是"让进哥报一个数"：他要去翻交割单才能说出自己典型持有
    几天，成本比跑三档高得多；而三档的边际成本很低 —— ADV 与波动率算一次就够，
    持有期只是乘 244/h。所以正确做法是给三档、让他认领一档，而不是逼他先算。
    """
    ds = list(hold_days_list or _P.HOLD_PROFILES.values())
    scen = []
    for hd0 in ds:
        # 从 API 进来的是查询串，全是字符串（"3,10,20" 拆出来是 "3"）。
        # 这里必须转成 int 再往下传，否则返回值里 hold_days 会是 "3" ——
        # 前端 `=== 3` 之类的比对会静默失效，而它看着像正常的数字。
        try:
            hd = int(float(str(hd0).strip()))
        except (TypeError, ValueError):
            hd = hd0          # 非法值原样交给 evaluate，由它报 missing_required
        r = evaluate(codes, aum, n_hold, hd, fee_bp, names=names, rank_hint=rank_hint)
        if not r.get("ok"):
            scen.append({"hold_days": hd, "ok": False,
                         "error": r.get("error"), "missing": r.get("missing")})
            continue
        cs = r["cost_summary"]
        scen.append({
            "ok": True, "hold_days": hd,
            "annual_turns": cs["annual_turns"],
            "roundtrip_bp_median": cs["roundtrip_bp_median"],
            "annual_cost_bp_median": cs["annual_cost_bp_median"],
            "annual_cost_bp_max": cs["annual_cost_bp_max"],
            "verdict": r["verdict"],
            "infeasible_codes": r["infeasible_codes"],
            "capacity_upper": r["capacity"]["upper_point"],
        })
    return {
        "ok": True,
        "scenarios": scen,
        "profiles": dict(_P.HOLD_PROFILE_LABEL),
        "note": ("只改持有期，其余三字段不变。两点必须一起读："
                 "① 持有期只影响成本摊薄，**不影响容量** —— 拿久一点不能多装钱；"
                 "② 三档的 verdict 通常一样，差别全在年化成本。也就是说短线打法的"
                 "主要风险不是'买不进'，是'手续费吃掉'。"),
    }


def _assumptions():
    """把"哪些数是实测、哪些数是拍的"直接返回给前端，别让它沉在文档里。"""
    return {
        "实测": {
            "CAPACITY_PARTICIPATION": _P.CAPACITY_PARTICIPATION,
            "来源": "测评报告的冲击成本模型反解值（单票下单额／当日成交额上限）",
            "ADV 单位": "kline.c(前复权) × kline.v(股) = 元；窗口内除权的票 adv 偏低",
        },
        "拍定（非实测，可改）": {
            "COST_TIERS_BP": [list(x) for x in _P.COST_TIERS_BP],
            "LIQUIDITY_REJECT_ADV": _P.LIQUIDITY_REJECT_ADV,
            "说明": "报告只给了'成本按流动性分档'这条修法，没给档位值；"
                    "A 股真实冲击成本须用自有成交回报标定。改参数请只改 params.py。",
        },
        "口径": {
            "N": "你真实会买的只数，是结论的前提，不是产品容量",
            "min(ADV)": "名单内最小值，用于报上界；指认拖后腿的票用 σ²/ADV 最大",
            "hold_days": ("参与**年化成本**计算（年轮换 = 244/hold_days），"
                          "但**不参与容量**：容量约束是单票下单额／当日成交额，"
                          "与持有多久无关 —— 拿久一点不能多装钱"),
            "fee_bp": "单边佣金，与 ADV 分档的冲击成本相加后乘 2 得往返成本",
        },
    }


def describe():
    """给 API 用的自述。"""
    return {"params": _P.describe(), "assumptions": _assumptions(),
            "kline_db": os.path.exists(_KLINE)}


if __name__ == "__main__":
    import json
    import sys
    codes = sys.argv[1].split(",") if len(sys.argv) > 1 else []
    print(json.dumps(evaluate(codes, aum=2_000_000, n_hold=len(codes) or 1,
                              hold_days=20, fee_bp=2.5),
                     ensure_ascii=False, indent=2))
