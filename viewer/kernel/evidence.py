# -*- coding: utf-8 -*-
"""信号证据层（2026-10-29）

## 为什么要有这一层

系统里同时存在两套互相矛盾的账本：

  A) `predictor_score` —— 每日预测被结算后的实盘命中率。每组样本几十到几百，
     标准误约 ±3~5 个百分点。用它区分信号好坏，等于用噪声校准信号。
  B) `signal_backtest` —— 全市场逐日条件回测，单条信号样本可达 76 万，
     标准误在 ±0.1 个百分点量级。

此前只有 A 参与权重计算，B 只用来打印一份控制台报告。本层的作用是把 B 整理成
`_compute_weights` 能消费的结构，并给出可读的裁决。

## 三个口径约束（都是实际踩过的坑）

1. **必须看超额胜率，不能只看绝对胜率。**
   A 股全样本前向 5 日胜率本身就在 47% 左右（下跌多于上涨）。一个 48% 的信号
   看着"过半"，实际跑输基线 —— 没有信息。

2. **必须看夏普，不能只看胜率。**
   胜率高但均值收益为负 = 小赚很多次、大亏少数次。这种信号加进排序会持续失血。

3. **样本不足必须显式说"不足以判断"。**
   不足 `params.JUDGE_MIN_N`(500) 个样本的信号不许拿来做决定，不允许拿几十个
   样本假装有效。这正是 `predictor_score` 里那些"T+5 +0.21% 但样本只有 1 个"的来源。
   2026-10-31 决策①起这段拆成两层：< 40 一律 insufficient（连方向都不给）；
   40–500 标 provisional（"观测中"）—— 方向可见，但 `usable=False`，
   不进组合、不改权重、不参与"强/中"判定。阈值不在本文件写死，见 params.tier_of。

## 与权重算法的关系

`tech_factor()` 返回一个连续乘数（0.5~1.3），供 `_compute_weights` 修正技术面路的
权重：大样本证明这类信号整体为负 → 降权；整体为正 → 升权。连续而非跳变，
便于回滚，也避免单次回测的噪声直接掀翻权重。
"""
import math
import sqlite3

# ---- 阈值（保守取值，改动前请先想清楚误判代价）-------------------------------
# 2026-10-31 整改（测评 P0-D）：MIN_N 不再在本文件写字面量，改从参数注册表读。
# 原来 500 写在这里、40 写在 predictor.py 的 eff 公式里，两处互不知情，
# 于是在 n=499 上系统同时说"没资格裁决"和"99.9996% 把握"。
# 现在两个阈值同一来源，任一模块再写新字面量即由 tools/audit_params.py 判红。
try:
    from viewer.kernel import params as _P
except Exception:  # noqa: BLE001 —— 无包上下文（viewer/ 直接进 sys.path）时退回相对导入
    # 2026-10-31 二审：这里原本写 `MIN_N = 500` 兜底，但那等于**把第二来源又种回来**——
    # 审计器判红是对的。兜底分支改成换一条导入路径，而不是换一个数字。
    from . import params as _P

# 兼容旧引用（有模块直接 from viewer.kernel.evidence import MIN_N）。
# 值一律来自 params，本文件不出现样本量字面量。
MIN_N = _P.JUDGE_MIN_N
EDGE_WR_GOOD = 0.02    # 超额胜率 ≥ +2pt 才算有边际
EDGE_WR_BAD = -0.02    # 超额胜率 ≤ -2pt 算跑输
SHARPE_GOOD = 0.30
SHARPE_BAD = -0.30
TECH_MIN_TOTAL = 20000  # 技术面聚合至少要这么大的总样本才允许改权重

LABEL = {
    "effective": "有效",
    "neutral": "中性",
    "adverse": "反向",
    "insufficient": "样本不足",
    # 2026-10-31 决策①：40–500 这一层不再笼统叫"样本不足"。它确实给不出
    # 可依赖的结论，但样本已经够看方向了 —— 所以叫"观测中"，并明确不进组合。
    "provisional": "观测中",
}

# indicators._derive_signals 的产出（已去掉 "<数值>" 占位符）。
# 只收技术指标类 —— 板块/政策/情绪等业务信号没有做过因子回测，不在此列，
# 否则会拿"没测过"冒充"测过了"。
TECH_BUY = {
    "RSI6超卖", "WR6超卖", "跌破BOLL下轨", "MACD零上金叉区",
    "KDJ超卖金叉", "PDI>MDI多头", "放量", "股价在Supertrend上", "CCI超卖",
}
TECH_SELL = {
    "RSI6超买", "WR6超买", "突破BOLL上轨", "MACD零下死叉区",
    "KDJ超买死叉", "PDI<MDI空头", "股价在Supertrend下", "CCI超买",
}


def _f(v, d=0.0):
    try:
        x = float(v)
        return d if (math.isnan(x) or math.isinf(x)) else x
    except (TypeError, ValueError):
        return d


def load_baseline(con: sqlite3.Connection) -> dict:
    """全样本基线：{fwd: {n, winrate, avg_ret, winrate_net, avg_ret_net, fee, updated}}。

    2026-10-29 起同时给税前/税后两套。**裁决一律用税后**——税后才是真的能拿到的钱；
    税前只留作对照，用来回答"这点超额是不是被交易成本吃掉了"。
    老库（还没跑新版回测）没有 *_net 列时，自动退回税前值，不会 KeyError。
    """
    out = {}
    try:
        cols = [d[1] for d in con.execute("PRAGMA table_info(signal_baseline)").fetchall()]
        has_net = ("winrate_net" in cols) and ("avg_ret_net" in cols)
        sel = ("fwd, n, winrate, avg_ret, updated, fee, winrate_net, avg_ret_net" if has_net
               else "fwd, n, winrate, avg_ret, updated, 0, NULL, NULL")
        for r in con.execute("SELECT " + sel + " FROM signal_baseline"):
            out[int(r[0])] = {
                "n": int(r[1] or 0), "winrate": _f(r[2]), "avg_ret": _f(r[3]),
                "updated": r[4] or "", "fee": _f(r[5]),
                "winrate_net": _f(r[6]) if r[6] is not None else _f(r[2]),
                "avg_ret_net": _f(r[7]) if r[7] is not None else _f(r[3]),
            }
    except sqlite3.Error:
        pass
    return out


def load_signals(con: sqlite3.Connection) -> dict:
    """{(reason, direction): {fwd: {n, winrate, avg_ret, sharpe, *_net}}}。"""
    out = {}
    try:
        cols = [d[1] for d in con.execute("PRAGMA table_info(signal_backtest)").fetchall()]
        has_net = ("winrate_net" in cols) and ("avg_ret_net" in cols)
        sel = ("reason, direction, fwd, n, winrate, avg_ret, sharpe, fee,"
               " winrate_net, avg_ret_net, sharpe_net" if has_net
               else "reason, direction, fwd, n, winrate, avg_ret, sharpe, 0, NULL, NULL, NULL")
        for r in con.execute("SELECT " + sel + " FROM signal_backtest"):
            key = (str(r[0]), str(r[1]))
            out.setdefault(key, {})[int(r[2])] = {
                "n": int(r[3] or 0), "winrate": _f(r[4]),
                "avg_ret": _f(r[5]), "sharpe": _f(r[6]),
                "fee": _f(r[7]),
                "winrate_net": _f(r[8]) if r[8] is not None else _f(r[4]),
                "avg_ret_net": _f(r[9]) if r[9] is not None else _f(r[5]),
                "sharpe_net": _f(r[10]) if r[10] is not None else _f(r[6]),
            }
    except sqlite3.Error:
        pass
    return out


def judge(n: int, edge_wr: float, avg_ret: float, sharpe: float) -> str:
    """单信号裁决。返回 effective / neutral / adverse / insufficient / provisional。

    2026-10-31 决策①：改成三层，三层判定统一走 `params.tier_of()`。

      n <  OBSERVE_MIN_N   → insufficient（连方向都不给）
      OBSERVE_MIN_N ≤ n < JUDGE_MIN_N → provisional（给方向，但调用方必须当它不可用）
      n ≥  JUDGE_MIN_N     → 原三分类

    阈值一律写符号名、不写数字：这行注释原本写着具体数值，被
    tools/audit_params.py 判为"阈值出现了第二个来源"—— 判得对，
    改了参数而注释不动，是比硬编码更难查的一种腐烂。

    为什么中间层不直接沿用 insufficient：那会让"已经看了 300 个样本、方向很清楚、
    但不够下结论"的信号和"只看了 3 个样本"的信号长得一模一样，
    于是既不能用来参考，也没人知道它差点就够了。provisional 把这两件事分开：
    **结论不可用，但观察状态可见**。

    这个函数的返回值**不代表可用性** —— 可用性看 `usable`（= tier == decidable）。
    """
    t = _P.tier_of(n)
    if t == _P.TIER_OBSERVE:
        return "insufficient"
    if t == _P.TIER_PROVISIONAL:
        return "provisional"
    if sharpe <= SHARPE_BAD and edge_wr <= 0:
        return "adverse"
    if edge_wr >= EDGE_WR_GOOD and sharpe >= SHARPE_GOOD:
        return "effective"
    return "neutral"


def tier_info(n) -> dict:
    """给接口/前端用的分层信息 —— **唯一入口**。

    为什么要单独一个函数：本轮发现 `server.py` 的 /api/signal_evidence 自己又
    算了一遍 verdict（没走 build()），如果只改 build()，那个面板就会成为
    全站唯一一个还是"两层"的地方 —— 同一套系统两个口径，正是本轮要治的病。
    以后任何地方要显示"这信号样本够不够"，只许调这里。
    """
    t = _P.tier_of(n)
    return {
        "tier": t,
        "tier_label": _P.TIER_LABEL[t],
        # usable 是**唯一**该被下游当门用的字段：它回答"能不能拿它做决定"。
        # verdict 回答"方向看起来怎样"，两者不是一回事。
        "usable": t == _P.TIER_DECIDABLE,
        "conf": round(_P.eff_of(n), 4),          # 样本量置信度（展示用）
        "weight_gate": round(_P.weight_gate(n), 4),
    }


def tier_legend() -> dict:
    """三层制的口径说明 —— 接口/前端/报表共用一份，不许各自复制一份文案。

    （复制文案的后果已经踩过：同一句"样本不足"在三个面板里有三种触发条件。）
    """
    return {
        "observe_below": _P.OBSERVE_MIN_N,
        "provisional": [_P.OBSERVE_MIN_N, _P.JUDGE_MIN_N],
        "decidable_at": _P.JUDGE_MIN_N,
        "label": dict(_P.TIER_LABEL),
        "rule": ("n<%d 不给方向；%d–%d 给方向但 usable=False"
                 "（不进组合、不参与强/中判定，权重按 gate 线性衰减）；n≥%d 可裁决。"
                 % (_P.OBSERVE_MIN_N, _P.OBSERVE_MIN_N, _P.JUDGE_MIN_N, _P.JUDGE_MIN_N)),
    }


def build(con: sqlite3.Connection, fwd_main: int = 5) -> dict:
    """完整证据包：基线 + 每信号战绩 + 技术面聚合。给 API 与权重算法共用。

    裁决口径（2026-10-29 改）：**税后**。税前超额里有一部分是成本幻觉——
    实测 T+1 平均仅 -0.586%，双边成本 0.15% 就足以把"微幅正超额"翻成负。
    """
    base = load_baseline(con)
    sigs = load_signals(con)
    b_main = base.get(fwd_main) or {}
    b_wr = _f(b_main.get("winrate"))
    b_ret = _f(b_main.get("avg_ret"))
    b_wr_net = _f(b_main.get("winrate_net")) if b_main.get("winrate_net") is not None else b_wr
    b_ret_net = _f(b_main.get("avg_ret_net")) if b_main.get("avg_ret_net") is not None else b_ret
    fee = _f(b_main.get("fee"))

    rows = []
    for (reason, direction), by_fwd in sigs.items():
        d = by_fwd.get(fwd_main)
        if not d:
            continue
        if reason not in TECH_BUY and reason not in TECH_SELL:
            continue          # 非技术类不参与（没有回测依据）
        wr_net = d.get("winrate_net", d["winrate"])
        ret_net = d.get("avg_ret_net", d["avg_ret"])
        sh_net = d.get("sharpe_net", d["sharpe"])
        edge_wr = wr_net - b_wr_net
        edge_ret = ret_net - b_ret_net
        v = judge(d["n"], edge_wr, ret_net, sh_net)
        rows.append({
            "reason": reason, "direction": direction, "fwd": fwd_main,
            "n": d["n"], "winrate": round(d["winrate"], 4),
            "avg_ret": round(d["avg_ret"], 4), "sharpe": round(d["sharpe"], 3),
            "winrate_net": round(wr_net, 4), "avg_ret_net": round(ret_net, 4),
            "sharpe_net": round(sh_net, 3),
            "edge_wr": round(edge_wr, 4), "edge_ret": round(edge_ret, 4),
            "verdict": v, "label": LABEL[v],
            # 决策①：tier / usable / conf / weight_gate 由 tier_info 统一给出，
            # 不在本文件手拼 —— 手拼就会长成第二来源。
            **tier_info(d["n"]),
        })
    rows.sort(key=lambda x: (x["direction"], -x["n"]))

    return {
        "ok": bool(rows),
        "baseline": base,
        "fwd": fwd_main,
        "signals": rows,
        "tech": tech_summary(rows),
        "tech_factor": round(tech_factor_of(rows), 3),
        "fee": round(fee, 6),
        "net": bool(fee > 0),
        "tiering": tier_legend(),
        "note": ("超额 = 信号胜率/收益 − 同期全样本基线；已按双边 %.2f%% 成本扣费（税后口径）。"
                 "样本分三层：<%d 不判方向；%d–%d 标「观测中」（给方向但不进组合、不改权重）；"
                 "≥%d 才可裁决。"
                 % (fee * 100, _P.OBSERVE_MIN_N, _P.OBSERVE_MIN_N, _P.JUDGE_MIN_N, _P.JUDGE_MIN_N))
                if fee > 0 else
                ("超额 = 信号胜率/收益 − 同期全样本基线；当前为税前口径。"
                 "样本分三层：<%d 不判方向；%d–%d 标「观测中」；≥%d 才可裁决。"
                 % (_P.OBSERVE_MIN_N, _P.OBSERVE_MIN_N, _P.JUDGE_MIN_N, _P.JUDGE_MIN_N)),
    }


def tech_summary(rows: list) -> dict:
    """技术面聚合：按样本量加权平均超额胜率与夏普（只看买入方向、只看可裁决的）。

    只看 buy：系统的用途是选股做多，卖出方向的历史表现有助于判断"该不该追"，
    但不能直接用来修正买入权重，混在一起会把结论搞反。
    2026-10-29：夏普改用税后值——税前夏普会系统性地高估，用它放大权重等于奖励成本幻觉。
    2026-10-31：口径从 `verdict != insufficient` 改为 `usable`（= tier 为 decidable）。
    原来只要不叫"样本不足"就进聚合，于是 40–500 的 provisional 信号会被算进
    技术面超额并改写权重 —— 那正是本轮要堵的口子。
    """
    # 各层计数先算 —— 因为它必须在"没有可裁决信号"时也能返回。
    # 原来这一句写在提前返回之后，于是当全部信号都落在观测中时，
    # 面板会显示"观测中 0 条"，而那正是最该显示数字的时刻。
    cnt = {"effective": 0, "adverse": 0, "neutral": 0, "insufficient": 0, "provisional": 0}
    for r in rows:
        cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1

    buys = [r for r in rows if r["direction"] == "buy" and r.get("usable")]
    if not buys:
        return {"n_total": 0, "edge_wr": 0.0, "sharpe": 0.0, "count": 0, **cnt}
    tot = sum(r["n"] for r in buys) or 1
    edge = sum(r["edge_wr"] * r["n"] for r in buys) / tot
    sh = sum(r.get("sharpe_net", r["sharpe"]) * r["n"] for r in buys) / tot
    return {
        "n_total": tot, "edge_wr": round(edge, 4), "sharpe": round(sh, 3),
        "count": len(buys), **cnt,
    }


def tech_factor_of(rows: list) -> float:
    """把技术面聚合结论转成权重乘数（0.5 ~ 1.3）。无足够样本时返回 1.0（不动）。

    连续函数而非阈值跳变：单次回测有噪声，跳变会让权重抖；连续乘数也让"这次改了
    多少、为什么"可以复盘。0.5 的下限是防呆 —— 技术面在十二路里是主要覆盖源，
    权重腰斩会让排序失去区分度。
    """
    t = tech_summary(rows)
    if t["n_total"] < TECH_MIN_TOTAL:
        return 1.0
    z = (t["edge_wr"] / 0.03) * 0.5 + (t["sharpe"] / 0.5) * 0.3
    return max(0.5, min(1.3, 1.0 + z))


def tech_factor(con: sqlite3.Connection, fwd: int = 5) -> float:
    """便捷入口：直接给权重算法调用。"""
    try:
        return tech_factor_of(build(con, fwd).get("signals") or [])
    except Exception:  # noqa: BLE001
        return 1.0


if __name__ == "__main__":
    import os
    import json
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer", "app.db")
    c = sqlite3.connect(p, timeout=20)
    print(json.dumps(build(c), ensure_ascii=False, indent=1)[:4000])
    c.close()
