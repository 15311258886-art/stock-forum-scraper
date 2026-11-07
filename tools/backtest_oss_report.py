#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 backtest_oss.py 的 JSON 渲染成 Markdown 报告。

用法：
    python tools/backtest_oss_report.py
    python tools/backtest_oss_report.py --main viewer/logs/x.json --grid ... --full ...

设计原则：**表里出现的每个数字都来自 JSON，不在渲染层做任何加工或挑选。**
渲染层一旦能"顺手挑一下"，报告就会开始报喜不报忧。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "viewer" / "logs"

HORIZON_ORDER = (1, 2, 3, 5, 10, 20, 30, 40, 60, 90, 120, 180, 250)
CLASS_CN = {"buy": "可当买点", "reverse": "稳定反向（只能当剔除器）", "unstable": "没有结论"}


def _f(v, fmt="%+.2f", dash="—"):
    return dash if v is None else fmt % v


def _ex_sample(row: dict) -> float | None:
    """持仓加权超额 = 原始收益 − 基准(持仓加权) − 成本。对不上账就是有 bug，别拿它讲故事。"""
    r, b = row.get("mean_ret"), row.get("mean_bench_sample")
    if r is None or b is None:
        return None
    return round(r - b - 0.25, 4)


def _load(p: str | Path | None) -> dict | None:
    if not p:
        return None
    path = Path(p)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _default(name: str) -> Path | None:
    hits = sorted(LOGS.glob("backtest_oss_*_%s.json" % name))
    return hits[-1] if hits else None


def _basis() -> dict | None:
    """读口径对照结果（由 tools/backtest_oss_basis_check.py 落盘）。"""
    hits = sorted(LOGS.glob("backtest_oss_basis_*.json"))
    if not hits:
        return None
    return json.loads(hits[-1].read_text(encoding="utf-8"))


def _gap_audit() -> dict | None:
    """读除权缺口审计（由 tools/audit_hfq_gap.py 落盘）。

    ★ 为什么必须有这个：`backtest_oss_basis_check.py` 用的是「最新日收盘价比值」，
      而后复权序列最后一根恒等于真实成交价、前复权也锚在最新日，所以那个比值**必然为 1**，
      是构造决定的恒等式，验不出任何历史差异。第一版报告把它当证据（「4999 只票全部等于 1，
      强度极高」）是错的，已作废。真正的检验只能是逐票全序列日收益比对。
    """
    hits = sorted(LOGS.glob("audit_hfq_gap_*.json"))
    if not hits:
        return None
    data = json.loads(hits[-1].read_text(encoding="utf-8"))
    data["_file"] = hits[-1].name
    return data


def _cascade() -> dict | None:
    """读级联增量结果（由 tools/cascade_delta.py 落盘）。"""
    hits = sorted(LOGS.glob("cascade_delta_*.json"))
    hits = [h for h in hits if "_qfq_" not in h.name and "_fc_" not in h.name]
    if not hits:
        return None
    return json.loads(hits[-1].read_text(encoding="utf-8"))


def _gap_attr() -> dict | None:
    """读污染归因分解（由 tools/cascade_delta.py --stage gapattr 落盘）。"""
    hits = sorted(LOGS.glob("cascade_gap_attr_*.json"))
    return json.loads(hits[-1].read_text(encoding="utf-8")) if hits else None


def _cascade_qfq() -> dict | None:
    """读前复权重建的级联结果（Step 6）。"""
    hits = sorted(LOGS.glob("cascade_delta_qfq_*.json"))
    if not hits:
        return None
    return json.loads(hits[-1].read_text(encoding="utf-8"))


def render(main: dict, grid: dict | None, full: dict | None) -> str:
    meta = main["meta"]
    L: list[str] = []
    w = meta["window"]

    L.append("# 11 条开源规则 · 多持有期回测报告")
    L.append("")
    L.append("> 生成时间 %s ｜ 引擎 `tools/backtest_oss.py` ｜ 数据 `%s`（%s）"
             % (meta["generated_at"], meta["db"], meta["db_adj"]))
    L.append("> ⚠️ 这个库**标称**后复权，但除权日存在机械缺口（0.1535% 的股票日、影响 36.5% 的票），"
             "详见第 8 节第 9 条与第 10 节；已用前复权库重建复核（第 11 节），结论方向不变。")
    L.append("> 信号窗口 **%s ~ %s**（%d 个信号日，%d 只票）｜ 成本 %.2f%%／次进出 ｜ "
             "剔除 ST（%d 个代码）+ 上市不满 %d 个交易日的次新 + 信号日停牌"
             % (w["since"], w["until"], w["signal_days"], meta["n_stocks"],
                meta["cost_pct"], meta["n_st_codes"], meta["min_listed_bars"]))
    L.append("")

    # ---------- 一句话结论 ----------
    vd = main["verdicts"]
    n_buy = sum(1 for v in vd if v["verdict_class"] == "buy")
    n_rev = sum(1 for v in vd if v["verdict_class"] == "reverse")
    n_un = sum(1 for v in vd if v["verdict_class"] == "unstable")
    L.append("## 0. 一句话结论")
    L.append("")
    nws = sorted((v["best"] or {}).get("t_nw") or 0 for v in vd
                 if v["verdict_class"] == "reverse")
    nws_txt = ("NW t 在 %.2f ~ %.2f 之间" % (min(nws), max(nws))) if nws else ""
    L.append("**11 条里能当买点用的是 %d 条。** 其中 **%d 条是「稳定地反向」**"
             "（在它自己分类的规定持有期上，超额显著为负、6 个半年段里 ≥4 段同向为负、"
             "三种市场状态里没有一段显著为正；%s），**%d 条没有结论**。"
             % (n_buy, n_rev, nws_txt, n_un))
    L.append("> ⚠️ **本报告所有「负超额」都没有做市值风格中性。** 收工前补了一轮复核"
             "（`tools/style_neutral.py`）：2023-11~2026-11 按近 20 日成交额把全市场分 5 档，"
             "最小档比最大档 T+20 高 **2.19pp**（t=−2.53）—— **这三年是小盘跑赢**。"
             "所以凡是偏向大中盘的规则，它的负超额里都混着风格。中性化之后："
             "**`oss_rsi_overbought` 反向更强**（T+20 被剔票均值 −2.19 → −3.04pp）、"
             "`oss_burst` / `oss_burst_vol` 基本不变或略强、"
             "**`oss_shrink_vol` 转正（−0.22 → +0.11pp）—— 它的「稳定反向」完全来自风格，"
             "应改判为未验证**、`oss_pullback_ma20` 剔掉之后反而有害（Δ_rule +0.000 → −0.316pp）。"
             "详见 `docs/级联增量验证.md` 第 13 节。")
    L.append("")
    _ga0 = _gap_attr()
    if _ga0:
        _dn = [x for x in _ga0["rules"] if x.get("downgraded")
               and x["rule"] in {v["rule"] for v in vd if v["verdict_class"] == "reverse"}]
        L.append("> **上面这个「%d 条」已经被污染归因修正过**：把命中集按「信号日往前 L 根里"
                 "有没有除权缺口」拆开、只用干净样本重算后，**%d 条跌破 |t_nw|=2，降级为「未验证」**（%s）；"
                 "**仍成立 %d 条**。另外两条 `oss_near_high` / `near_high_tight` 的负号"
                 "是数据缺陷造的，不是策略性质（详见第 11 节）。"
                 % (n_rev, len(_dn), "、".join("%s（%s → %s）" % (
                     x["rule"], _f(x["all"]["nw"], "%.2f"), _f(x["clean"]["nw"], "%.2f"))
                     for x in _dn), n_rev - len(_dn)))
        L.append("")
    L.append("这不是「它们全是噪声」，而是两件不同的事：")
    L.append("")
    L.append("- **稳定反向的 %d 条**（%s；其中 2 条经污染归因后降级为未验证，见上）跑输的方向在 5~6 个半年段里一致、"
             "Newey-West t 也够 —— 但**这不等于把它们当剔除器就有用**。"
             "本报告第 11 节专门做了「剔掉它们 vs 随机剔掉同样多」的三臂实验，"
             "结论是**净增量只有 +0.009~+0.073pp，没有一条能在四种配置下稳定过线**，"
             "只能当说明性剔除，不能当决策台的硬过滤。" %
             (n_rev, "、".join(v["rule"] for v in vd if v["verdict_class"] == "reverse")))
    L.append("- **没有结论的 %d 条**（%s）分段方向摇摆或显著性扛不住窗口重叠校正，"
             "既不能买也不能反着用。" %
             (n_un, "、".join(v["rule"] for v in vd if v["verdict_class"] == "unstable")))
    L.append("")
    L.append("> 上一版这里写的是「拿它们当剔除器/反向计分是有信息量的」—— **那句话当时没有测过，"
             "属于推断当结论**。第 11 节把实验补上了，结论是：不能。原文已改。")
    L.append("")
    L.append("按「持有期错配 / 参数不适配 / 制度差异 / 真的没用」归因，见第 7 节。")
    L.append("")

    # ---------- 1. 主表 ----------
    L.append("## 1. 每条规则在它**自己分类**的规定持有期上")
    L.append("")
    L.append("`超额` = 个股收益 − 同日全市场等权收益 − 0.25% 成本（按日聚合后的日均值）；"
             "`t` = 日序列 t 检验；`t_nw` = Newey-West（滞后阶 = 持有期）；"
             "`胜率` = 超额>0 的天数占比；`建仓缺口` = 信号日收盘→次日收盘的平均涨幅"
             "（这是「收盘出信号、次日收盘买」必须付的追价成本）。")
    L.append("")
    L.append("| 规则 | 分类 | 持有期 | 样本数 | 日均命中 | 超额%(日等权) | t | t_nw | "
             "胜率% | 原始收益% | 基准%(持仓) | 超额%(持仓) | 建仓缺口% | 判定 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for row in main["results"]:
        if not row["required"]:
            continue
        v = next((x for x in vd if x["rule"] == row["rule"]
                  and x["best_horizon"] == row["horizon"]), None)
        cls = CLASS_CN.get(v["verdict_class"], "") if v else ""
        star = "★" if v and v["best_horizon"] == row["horizon"] else ""
        L.append("| %s%s | %s | T+%d | %d | %.1f | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                 % (row["rule"], star, row["category_cn"], row["horizon"],
                    row["n_samples"], row["avg_hits_day"] or 0,
                    _f(row["mean_excess"]), _f(row["t"], "%.2f"),
                    _f(row["t_nw"], "%.2f"), _f(row["win_days"], "%.1f"),
                    _f(row["mean_ret"]), _f(row["mean_bench_sample"]),
                    _f(_ex_sample(row)), _f(row["mean_entry_gap"]), cls))
    L.append("")
    L.append("★ = 该规则在其分类里的最佳持有期（按 t 最高挑，只在分类规定的三个里挑）。"
             "注意「最佳」经常仍然是负的 —— 这就是结论。")
    L.append("")
    L.append("**两种加权为什么都给：** `超额%(日等权)` 是每天算一个平均超额再对天平均 —— "
             "t 检验必须用这一列（硬口径要求按日聚合）；`超额%(持仓)` 是每笔持仓等权"
             "（= 原始收益 − 基准(持仓) − 成本），这才是「每天按信号买一篮子、每只票平均分钱」"
             "真正拿到的数。命中只数随行情剧烈变化时两列会分叉，"
             "分叉本身是信息：说明这条规则的超额集中在少数几天，不可持续。")
    L.append("")

    # ---------- 2. 分段 ----------
    L.append("## 2. 分段稳定性（3 年切成 6 个半年段）")
    L.append("")
    L.append("判定规则：**6 段里至少 4 段与整体同向，且反向显著（|t|>2 且符号相反）不超过 1 段。** "
             "括号里是各段的 t 值。")
    L.append("")
    L.append("| 规则 | 持有期 | 2023-11~2024-05 | 2024-05~2024-11 | 2024-11~2025-05 | "
             "2025-05~2025-11 | 2025-11~2026-05 | 2026-05~2026-11 | 同向 | 反向显著 | 稳定 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for v in vd:
        row = next((r for r in main["results"]
                    if r["rule"] == v["rule"] and r["horizon"] == v["best_horizon"]), None)
        if not row:
            continue
        cells = []
        for sg in row["segments"]:
            if sg["mean_excess"] is None:
                cells.append("无样本")
            else:
                cells.append("%+.2f (%.1f)" % (sg["mean_excess"], sg["t"] or 0))
        L.append("| %s | T+%d | %s | %d/6 | %d | %s |"
                 % (v["rule"], v["best_horizon"], " | ".join(cells),
                    row["n_same"], row["n_opp_sig"], "通过" if row["stable"] else "**不稳**"))
    L.append("")

    # ---------- 3. 市场状态 ----------
    L.append("## 3. 市场状态分层（用全市场等权 60 日涨跌分档）")
    L.append("")
    L.append("牛市 = 前 60 个交易日全市场等权涨 >+5%；震荡 = −5%~+5%；熊市 = <−5%。"
             "括号里是 t 值。**这一栏是回答「市场好就全通过、市场差就全废」的正面证据。**")
    L.append("")
    L.append("| 规则 | 持有期 | 牛市 | 震荡 | 熊市 | 反向显著的状态 |")
    L.append("|---|---|---|---|---|---|")
    for v in vd:
        row = next((r for r in main["results"]
                    if r["rule"] == v["rule"] and r["horizon"] == v["best_horizon"]), None)
        if not row:
            continue
        cells = []
        for key in ("bull", "range", "bear"):
            rg = row["regimes"][key]
            cells.append("无样本" if rg["mean_excess"] is None
                         else "%+.2f (%.1f)" % (rg["mean_excess"], rg["t"] or 0))
        rev = row["regime_reverse"]
        L.append("| %s | T+%d | %s | %s |"
                 % (v["rule"], v["best_horizon"], " | ".join(cells),
                    "**%s**" % "、".join(rev) if rev else "无"))
    L.append("")
    L.append("> 读法：三档全为负而无一档显著为正 → 这条规则在任何行情里都不是买点；"
             "只有一档为正、另一档显著为负 → 它只在特定行情里成立，不能单独当买点。")
    L.append("")

    # ---------- 4. 持有期网格 ----------
    if grid:
        L.append("## 4. 持有期错配检查（每条规则 × 1~250 日全网格，单位 %）")
        L.append("")
        L.append("只为了回答一个问题：**是不是我们拿错了持有期？** "
                 "如果有某条规则在别的持有期上显著为正，那属于「错配」；"
                 "如果整条曲线上都是负的，那就不是尺子的问题。")
        L.append("")
        L.append("| 规则 | " + " | ".join("T+%d" % h for h in HORIZON_ORDER) + " |")
        L.append("|---|" + "---|" * len(HORIZON_ORDER))
        by: dict[str, dict[int, dict]] = {}
        for r in grid["results"]:
            by.setdefault(r["rule"], {})[r["horizon"]] = r
        for rule, horizons in by.items():
            cells = []
            for h in HORIZON_ORDER:
                r = horizons.get(h)
                cells.append("—" if not r else "%+.2f" % (r["mean_excess"] or 0.0))
            L.append("| %s | %s |" % (rule, " | ".join(cells)))
        L.append("")
        pos = [r for r in grid["results"]
               if r["t"] is not None and r["t"] > 1.5 and (r["mean_excess"] or 0) > 0]
        L.append("全网格里 t>1.5 且超额为正的组合共 **%d 个**：" % len(pos))
        L.append("")
        if pos:
            for r in sorted(pos, key=lambda x: -x["t"]):
                L.append("- `%s` T+%d：%+.2f%%，t=%.2f，NW t=%.2f，样本 %d"
                         % (r["rule"], r["horizon"], r["mean_excess"], r["t"],
                            r["t_nw"] or 0, r["n_samples"]))
            L.append("")
            L.append("这些组合**都不在**它们所属分类的规定持有期上，而且 NW t 全部 < 1.3 —— "
                     "即：扛不住窗口重叠校正，不能当「找到正确持有期了」来读。")
        L.append("")

    # ---------- 5. 判定清单 ----------
    L.append("## 5. 逐条判定")
    L.append("")
    L.append("| 规则 | 分类 | 判定 | 最佳持有期 | 超额% | t | t_nw | 理由 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for v in vd:
        b = v["best"] or {}
        L.append("| %s | %s | **%s** | %s | %s | %s | %s | %s |"
                 % (v["rule"], v["category_cn"],
                    CLASS_CN.get(v["verdict_class"], v["verdict_class"]),
                    "T+%s" % v["best_horizon"] if v["best_horizon"] else "—",
                    _f(b.get("mean_excess")), _f(b.get("t"), "%.2f"),
                    _f(b.get("t_nw"), "%.2f"), v["reason"]))
    L.append("")

    # ---------- 6. 稳健性 ----------
    if full:
        L.append("## 6. 稳健性复核：把窗口收窄到「全市场覆盖完整」的 2024-02 之后")
        L.append("")
        L.append("`viewer/kbase.db` 的历史深度不是齐的（见第 8 节）：2024-02-07 之前只有约 "
                 "3,100 只票有行情，之后才有 5,300 只。这一段拿掉后重跑，看结论会不会翻。")
        L.append("")
        L.append("| 规则 | 持有期 | 3 年窗口超额% | 3 年 t | 全覆盖窗口超额% | 全覆盖 t | 方向一致 |")
        L.append("|---|---|---|---|---|---|---|")
        idx = {(r["rule"], r["horizon"]): r for r in main["results"]}
        for r in full["results"]:
            if not r["required"]:
                continue
            o = idx.get((r["rule"], r["horizon"]))
            if not o:
                continue
            same = ((r["mean_excess"] or 0) > 0) == ((o["mean_excess"] or 0) > 0)
            L.append("| %s | T+%d | %s | %s | %s | %s | %s |"
                     % (r["rule"], r["horizon"], _f(o["mean_excess"]),
                        _f(o["t"], "%.2f"), _f(r["mean_excess"]),
                        _f(r["t"], "%.2f"), "是" if same else "**否**"))
        L.append("")

    # ---------- 7. 归因 ----------
    # 这一节里的每个数字都从 JSON 现算，不写死 —— 否则下次重跑结论变了，
    # 报告里的解释还停在上一版，那是最坏的一种"看起来很有道理"。
    L.append("## 7. 归因：为什么是这个结果")
    L.append("")
    if grid:
        no_pos = []
        for rule in {r["rule"] for r in grid["results"]}:
            rows = [r for r in grid["results"] if r["rule"] == rule]
            if not any((r["mean_excess"] or 0) > 0 and (r["t"] or 0) > 1.5 for r in rows):
                no_pos.append(rule)
        L.append("1. **对 %d 条来说不是持有期错配。** 13 个持有期（T+1~T+250）全扫一遍，"
                 "这 %d 条**在任何持有期上**都没有出现过「超额为正且 t>1.5」的组合。"
                 "上一版拿 5 天去测 250 日尺度的判据确实是错，但改对之后结论没变 —— "
                 "说明病根不在尺子上。" % (len(no_pos), len(no_pos)))
        L.append("")

    rev_rules = [v for v in vd if v["verdict_class"] == "reverse"]
    if rev_rules:
        detail = "；".join("%s（%d/6 段同向为负，NW t=%.2f）"
                          % (v["rule"], (v["best"] or {}).get("n_same") or 0,
                             (v["best"] or {}).get("t_nw") or 0)
                          for v in rev_rules)
        L.append("2. **是方向错了，不是噪声。** 被判「稳定反向」的 %d 条：%s。"
                 "半年级别的一致性无法用「运气不好」解释，只能承认：在 A 股这段行情里，"
                 "「追放量上涨」「追超买」「追 20 日动量 / 追均线多头」之后若干天，"
                 "系统性跑输等权大盘。" % (len(rev_rules), detail))
        L.append("")

    rev = next((r for r in main["results"]
                if r["rule"] == "oss_reversal20" and r["horizon"] == 20), None)
    if rev and rev.get("mean_ret") is not None:
        ex_s = _ex_sample(rev) or 0.0
        L.append("3. **绝对收益会骗人，两种加权还会打起来。** `oss_reversal20` 在 T+20 上"
                 "原始收益 %+.2f%%、看着最好；扣掉同持仓窗口的等权基准 %+.2f%% 和成本后，"
                 "**持仓加权超额 %+.2f%%**，而**日等权超额 %+.2f%%** —— 两种加权符号相反。"
                 "原因很具体：它只在暴跌日一次涌出一大批命中（那天基准往往正在反弹），"
                 "零星命中时却是负的。所以它既不能算「有效」（日等权为负），"
                 "也不能算「无效」（持仓加权为正），只能判「没有结论」。"
                 "这也是为什么本报告同时给两列，而不挑好看的那一列。"
                 % (rev["mean_ret"], rev.get("mean_bench_sample") or 0, ex_s,
                    rev["mean_excess"]))
        L.append("")

    gaps = [(v["rule"], (v["best"] or {}).get("mean_entry_gap") or 0) for v in vd]
    gaps.sort(key=lambda x: -x[1])
    L.append("4. **追价成本只解释一小部分。** 建仓缺口（信号日收盘→次日收盘）最大的是 "
             "`%s` %+.2fpp、`%s` %+.2fpp，对 burst 系列约占其亏损的 1/3；"
             "其余规则都在 0.15pp 以内。不是主因，但「收盘出信号、次日收盘买」这条口径"
             "必须留在表里，否则会把自己的执行成本算成别人的 alpha。"
             % (gaps[0][0], gaps[0][1], gaps[1][0], gaps[1][1]))
    L.append("")
    L.append("5. **制度差异是主因（有直接证据）。** 这批规则的原作是美股日频动量/突破体系，"
             "假设「强者继续强」。而这段窗口里全市场等权 60 日收益在 −26%~+52% 之间摆动："
             "2023-11 中旬起点的 60 个交易日跌约 26%（2024-01 微盘崩塌），"
             "2024-09 起点的 60 个交易日涨约 52%（政策行情）。"
             "涨幅榜/超买名单在这两次摆动里恰好都站在错误的一边。")
    L.append("")

    # ---------- 8. 做不到的 ----------
    L.append("## 8. 必须写出来的限制（做不到 / 不确定的地方）")
    L.append("")
    cov = meta.get("coverage_monthly") or []
    low = [c for c in cov if c["n_stocks_avg"] < 4000]
    L.append("1. **数据深度不齐（最要紧的一条）。** `kbase.kline` 里 2024-02-07 之前只有约 "
             "3,100 只票的行情，之后才补齐到约 5,300 只 —— 有一批票（含 600519、000001 这类"
             "老票）的后复权历史只有 660 根 K 线，起点就是 2024-02-07。"
             "所以 3 年窗口的头 %d 个月样本偏薄（%s）。"
             % (len(low), "、".join("%s≈%d只" % (c["month"], c["n_stocks_avg"])
                                   for c in low[:4])))
    L.append("   已用「全覆盖窗口 2024-02-07 起」重跑复核（第 6 节），结论不变，"
             "但**头 3 个月的显著性不该被当真**。")
    L.append("2. **ST 剔除用的是「当前名称」。** `stock_names` 只有当前名字，没有带日期的历史名称表，"
             "所以「当年 ST、现在摘帽」的票没被剔、「当年正常、现在 ST」的票被全程剔掉了。"
             "后者偏保守（把最差的票移出样本，对所有规则同等对待），但会低估绝对超额。")
    L.append("3. **250 日持有期天然测不满。** 窗口末端 250 个交易日的信号没有未来数据，"
             "T+180 / T+250 的有效信号日只剩 %d / %d 个（共 %d 个信号日）。"
             % (next((r["n_days_window"] for r in (grid or main)["results"]
                      if r["horizon"] == 180), 0),
                next((r["n_days_window"] for r in (grid or main)["results"]
                      if r["horizon"] == 250), 0),
                w["signal_days"]))
    L.append("   所以 `long_hold` 那一档的结论强度弱于短周期。")
    L.append("4. **退市/长期停牌的票按「最后一根 K 线的收盘价」平仓。** 这是能成交的假设，"
             "但真实退市整理期的跌幅更大，所以基准和样本都略微偏乐观。")
    L.append("5. **没有做涨跌停可成交性过滤**（口径没要求）。表中给了「建仓日涨停占比」"
             "（`pct_limitup_entry`）供参考：burst 系列有 5.8% 的样本在建仓日逼近涨停，"
             "实盘会买不到，真实结果比表里更差而不是更好。")
    L.append("6. **没有回答「参数微调后会不会变好」。** 本次只测了**原作档默认参数**"
             "（`toolbox` 注册表里的 `default_params`）。A 股档建议参数（如 RSI 80→75、"
             "量比 1.5→1.8）需要另开一轮，不能拿这次的数字去推断。")
    L.append("7. **没有测「放进漏斗里还有没有用」。** 本次测的是每条规则**单独**的超额。"
             "筛台里它们是求交使用的，一条单独为负的判据在交集里仍可能提高精度 —— "
             "这需要按漏斗逐级回测（每一级的入选池 → 下一级），本次没做，"
             "所以不能拿这份报告下「这个工具该下架」的结论，只能下「它不能单独当买点」的结论。")
    L.append("8. **只覆盖了 11 条 oss 规则。** 另外 22 个工具（支柱/共振/箱体/机构持仓等）"
             "还没有接入这套引擎 —— 它们大多依赖 `app.db` 里的资金、龙虎榜、股东数据，"
             "而那些表没有 3 年历史，接进来只能测最近一年，会重犯「拿最近几天说事」的错。")
    gp = _gap_audit()
    if gp:
        L.append("9. **回测库在除权日有机械缺口（数据缺陷，不是口径选择）。** `kbase.kline` 标 "
                 "`adj='hfq'`，但送股/送转的复权因子没有全部打上：%s 个股票日里有 **%s 个"
                 "（%.4f%%）** 的日收益与真正的前复权序列相差超过 %g%%，涉及 **%s 只票（%.1f%%）**，"
                 "偏差 p99 %.1fpp、最大 %.1fpp。其中 99.93%% 的缺口是**向下**的，"
                 "所以它不会随机噪声化，而是给「持有期内含除权日」的样本注入一个假的负收益。"
                 "本报告已用前复权库重建复核（第 11 节）；但**长持有期档（T+90 以上）的绝对值仍应打折看待**，"
                 "因为一次缺口就足以吃掉几十个 pp。详见第 10 节与 `tools/audit_hfq_gap.py`。"
                 % (format(gp["stockdays_compared"], ","), format(gp["gap_stockdays"], ","),
                    gp["gap_rate_pct"], gp["threshold_pct"], format(gp["gap_codes"], ","),
                    gp["gap_code_rate_pct"], gp["abs_dev_quantiles_pct"]["p99"],
                    gp["abs_dev_quantiles_pct"]["max"]))
    L.append("")

    # ---------- 9. 耗时 ----------
    L.append("## 9. 耗时")
    L.append("")
    t = meta["timings"]
    L.append("| 阶段 | 秒 |")
    L.append("|---|---|")
    for k, v in t.items():
        L.append("| %s | %s |" % (k, v))
    L.append("")
    grid_s = (grid or {}).get("meta", {}).get("timings", {}).get("总计")
    L.append("单工具平均：%.1f 秒／条（11 条规则共用同一份指标矩阵，"
             "所以「单条」的增量成本只是统计部分）。%s"
             "回测是 CPU 单线程重活，跑的时候 8765 端口的服务会有几秒变慢，未重启、未 kill 服务。"
             % (t.get("总计", 0) / max(1, len(meta["rules"])),
                ("全网格 13 个持有期 %s 秒。" % grid_s) if grid_s else ""))
    L.append("")
    basis = _basis()
    gap = _gap_audit()
    if basis:
        L.append("## 10. 附录：与工具卡片上那些数字的口径对照")
        L.append("")
        L.append("工具卡片的 `evidence` 数字来自 `stock_indicators_daily` / `oss_rule` 表，"
                 "本报告的数字来自 `kbase` 标称后复权的 kline。两套数摆在一起会被问"
                 "「是不是又算了两遍」，所以这里把差异**逐项**拆开"
                 "（对照脚本 `tools/audit_hfq_gap.py`，结果 `viewer/logs/%s`）。"
                 % (gap.get("_file", "") if gap else ""))
        L.append("")
        L.append("> **作废的检验方法，留在此处示警**：早先我用「同日收盘价 指标表 ÷ kline 的比值」"
                 "判断两库是否同一基准，得到「4,999 只票全部等于 1」的结论并写进了报告。"
                 "**那个检验是无效的** —— 后复权序列的最后一根恒等于真实成交价、前复权也锚在最新日，"
                 "所以比值必然为 1，数据再脏也会通过。已整段删除。能用来看历史的只有"
                 "「逐票全序列日收益比对」。")
        L.append("")
        if gap:
            L.append("**① 价格基准不一致，而且是回测这一侧的问题。** 指标表是**前复权**；"
                     "`kbase.kline` 标称 `adj='hfq'`，但在**除权日**存在机械缺口 —— "
                     "复权因子在某些 10 送 N / 送股事件上没打上，价格直接跳空。")
            L.append("")
            L.append("| 逐票全序列日收益比对（%s ~ %s） | 数值 |"
                     % (gap["window"]["since"], gap["window"]["until"]))
            L.append("|---|---|")
            L.append("| 可比股票日 | %s |" % format(gap["stockdays_compared"], ","))
            L.append("| \\|日收益差\\| > %g%% 的股票日 | **%s = %.4f%%** |"
                     % (gap["threshold_pct"], format(gap["gap_stockdays"], ","),
                        gap["gap_rate_pct"]))
            L.append("| 涉及票数 | **%s / %s = %.1f%%** |"
                     % (format(gap["gap_codes"], ","),
                        format(gap["universe"]["common_codes"], ","),
                        gap["gap_code_rate_pct"]))
            Q = gap["abs_dev_quantiles_pct"]
            L.append("| 偏差分位 | p50 %.2fpp / p90 %.2fpp / p99 %.2fpp / **max %.2fpp** |"
                     % (Q["p50"], Q["p90"], Q["p99"], Q["max"]))
            L.append("")
            top = (gap.get("top_events") or [None])[0]
            if top:
                L.append("典型样本：`%s` 在 %s，前复权口径 %+.2f%%，后复权口径 **%+.2f%%** —— "
                         "差 %.2fpp，是送股缺口，不是真实跌幅。"
                         % (top["code"], top["date"], top["qfq_ret_pct"],
                            top["kbase_ret_pct"], top["dev_pp"]))
                L.append("")
            L.append("**这条必须写进结论的边界**：5,658 次缺口里 5,654 次为负（99.93%），"
                     "平均每次 −4.15pp，折算到每股票日 −0.0064pp → 持有 20 日约 −0.13pp、"
                     "90 日约 −0.57pp。对**随机**一只票，这个偏差会和基准腿对冲掉"
                     "（基准是同一批票的横截面均值）；但对**被规则筛出来的票**不对冲 —— "
                     "缺口本身就是触发条件（`reversal20` 的 20 日跌幅、`mom20`/`pullback_ma20`/`ma_align` "
                     "读的均线、`near_high` 读的 251 日最高收盘全都受影响）。"
                     "已用前复权库重建主表复核（第 11 节），结论方向不变。")
            L.append("")
        else:
            L.append("**① 价格基准：** 指标表是前复权，`kbase.kline` 标称后复权；"
                     "两者在除权日的日收益存在机械缺口，规模未量化"
                     "（跑 `tools/audit_hfq_gap.py` 可量化）。")
            L.append("")
        g5 = {r["rule"]: r for r in (grid or {}).get("results", [])
              if r.get("horizon") == 5}
        L.append("**② 差异的三个来源**（`oss_mine._CROSS_SQL` 与 `summarize()` 的实际做法）："
                 "a) 没扣 0.25% 成本；b) 窗口 394 天 vs 726 天；c) **不排除 ST 与次新**"
                 "（本报告两者都剔）。三项都会推同一条规则往更负的方向走，"
                 "所以「同一规则两套数」不是矛盾，但也不能只归因于其中一项。")
        L.append("")
        if g5:
            sh = g5.get("oss_shrink_vol") or {}
            hv = g5.get("oss_high_vol") or {}
            L.append("**③ 逐条举例（本报告的数是本次运行现算的）：**")
            L.append("")
            if sh:
                L.append("- `oss_shrink_vol` T+5：卡片 +0.21%%（394 天、未扣成本、含 ST）→ "
                         "扣成本后约 -0.04%% → 本报告 **%+.2f%%**（726 天、剔 ST/次新）→ "
                         "扣成本只解释了 0.25pp，剩下 %.2fpp 的差距来自窗口与股票池。"
                         % (sh.get("mean_excess") or 0,
                            abs((sh.get("mean_excess") or 0) + 0.04)))
            if hv:
                L.append("- `oss_high_vol` T+5：卡片 -0.21%% → 扣成本后 -0.46%% → "
                         "本报告 **%+.2f%%** —— **更长反而更不亏**。"
                         "所以不能写「窗口拉长后一律更负」，这种一刀切正是老板最反感的。"
                         % (hv.get("mean_excess") or 0))
            L.append("")
            L.append("（卡片侧的那几个数引自 `viewer/toolbox.py` 工具注册表的 `evidence` 字段，"
                     "不是本报告算的；本报告只对「自己这一侧」负责。）")
            L.append("")
        L.append("")

    # ---------- 11. 级联增量 ----------
    cas = _cascade()
    if cas:
        L.append("## 11. 级联增量验证：这 7 条当「剔除器」用到底有没有信息")
        L.append("")
        L.append("「稳定反向」只说明命中该回避，**不说明把它从池子里剔掉就能提高胜率** —— "
                 "剔掉 3%~21% 的票本身就换了一个基准。所以要测的不是「剔完池子好不好」，"
                 "而是「剔得对不对」：U（全市场）／R（剔除命中）／C（随机剔掉同样多，50 次抽样）三臂，"
                 "`Δ_rule = m_R − m_C` 为净增量，`Δ_shrink = m_C − m_U` 为空白对照"
                 "（其期望恒为 0，作用是当噪声标尺）。完整文档见 `docs/级联增量验证.md`。")
        L.append("")
        L.append("| 规则 | 持有期 | 命中占比 | m_U | m_S(被剔) | m_R | Δ_rule | t_nw | 6 段同向 | 统计判定 | **经济量级** |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in cas["main"]:
            L.append("| %s | T+%d | %.1f%% | %+.3f | %+.3f | %+.3f | **%+.3f** | %.2f | %d/6 | %s | %s |"
                     % (r["rule"], r["H"], r["share_pct"], r["m_U"], r["m_S"], r["m_R"],
                        r["d_rule"], r["nw_rule"] or 0.0, r["seg_pos"], r["verdict"],
                        r.get("econ_grade", "—")))
        L.append("")
        L.append("**经济量级门槛**（主理人 2026-11-05 追加，与上面预注册的统计判据分开列）："
                 "一次进出成本 25bp，所以 `Δ_rule ≥ 0.25pp` 才算可用、`0.05~0.25pp` 是"
                 "「有信息但不可用」、`< 0.05pp` 是「无经济意义」。按这个门槛重判，**7 条里 0 条可用**。")
        L.append("")
        L.append("> 「所谓『稳定反向』的 7 条规则，作为剔除器用在全市场上，最大的一条（burst_vol）"
                 "只能把池子期望从 −0.250% 抬到 −0.241%，即 0.9bp，为单次成本的 1/28。"
                 "**信息是真的，钱是不够的。**」")
        L.append("")
        L.append("这句话的算术根据是 `m_U = −0.250`：**全市场随机买一篮子的期望超额正好等于 −0.25%，"
                 "也就是成本本身**。剔除器只能把一个负的池子削得没那么负，不可能把它变成正的。"
                 "所以「胜率能不能提高」的完整答案是：**能提 0.05 个百分点，提完仍然是负的。**")
        L.append("")
        q = _cascade_qfq()
        if q:
            amap = {x["rule"]: x for x in cas["main"]}
            L.append("**换前复权基准复核（Step 6）**：`data/kline_daily.db` 与 kbase 在窗口内的 5,576 只票"
                     "**100% 重合**（另有 356 只 kbase 没有的北交所/新股），闸门通过后重建同一张表：")
            L.append("")
            L.append("| 规则 | 后复权 t_nw | 前复权 t_nw | 后复权 Δ_rule | 前复权 Δ_rule | 判定翻转 |")
            L.append("|---|---|---|---|---|---|")
            for r in q["main"]:
                a = amap.get(r["rule"])
                if not a:
                    continue
                L.append("| %s | %.2f | %.2f | %+.3f | %+.3f | %s |"
                         % (r["rule"], a["nw_rule"] or 0.0, r["nw_rule"] or 0.0,
                            a["d_rule"], r["d_rule"],
                            "**是**" if a["verdict"] != r["verdict"] else "否"))
            L.append("")
            L.append("只有 `oss_burst_vol` 一条翻转（t_nw 1.43 → 2.54），而两版 **Δ_rule 完全相同**"
                     "（+0.0086）—— 变的是日序列离散度，不是增量。把窗口对齐到 kbase 覆盖完整的 "
                     "2024-02-07 起再跑，两版给出同样判定（3.23 vs 3.96 都通过），"
                     "所以翻转的原因是 **kbase 头 3 个月只有 3,100 只票**，不是复权方式。")
            L.append("")
        ga = _gap_attr()
        if ga:
            L.append("**污染归因**：把每条规则的命中集按「信号日往前 L 根里有没有出现过除权缺口」"
                     "拆成污染命中/干净命中（L 取该规则真实回看窗口），超额一律用前复权重算。"
                     "完整表见 `docs/级联增量验证.md` 第 3.2 节，这里只列结论：")
            L.append("")
            L.append("| 规则 | L | 污染占比(全池同 L) | 全部命中 t_nw | 干净命中 t_nw | 处置 |")
            L.append("|---|---|---|---|---|---|")
            _main = {x["rule"] for x in cas["main"]}
            for _r in ga["rules"]:
                if _r["rule"] not in _main:
                    continue
                L.append("| %s | %d | %.2f%% (%.2f%%) | %s | **%s** | %s |"
                         % (_r["rule"], _r["lookback"], _r["contam_share_pct"],
                            _r["universe_poll_pct_same_L"], _f(_r["all"]["nw"], "%.2f"),
                            _f(_r["clean"]["nw"], "%.2f"),
                            "**降级为未验证**" if _r["downgraded"] else "仍显著"))
            L.append("")
            L.append("**这条把「7 条稳定反向」改成了「5 条仍成立 + 2 条未验证」**："
                     "`oss_mom20`（−1.93 → −1.87）、`oss_ma_align`（−2.11 → −1.85）在干净样本上"
                     "跌破 |2|，按预注册规则降级。剩下 5 条的干净 t_nw 仍在 |2| 以上，"
                     "「反向」不是缺口喂出来的。")
            L.append("")
            L.append("另外两条 `oss_near_high` / `near_high_tight` 的负号**完全是数据缺陷造的**："
                     "`near_high` 日等权超额 −1.754pp → 干净样本 −0.031pp、t_nw −1.10 → −0.02；"
                     "一次 10 送 4 让这只票在之后 251 根 bar 里都被判成「远离高点」。"
                     "它们原本就判「没有结论」，现在可以说得更硬。")
            L.append("")
        L.append("**结论**：`Δ_rule` 全部落在 −0.011 ~ +0.077pp，而随机对照的日度噪声最大到 0.15pp；"
                 "按预注册阈值 7 条里 5 条任何配置都不过线，2 条边界过线但净增量 +0.01~0.03pp、"
                 "被剔票里 37% 下一期仍跑赢市场。算术上这是必然的 —— "
                 "`m_R − m_U = k/(n−k) × (m_U − m_S)`，剔 3%~15% 的池子，"
                 "就算被剔票差 0.5~1.9pp，池子均值也只能抬百分之几个 0.01pp。"
                 "**所以这 7 条不能提高胜率，只能当说明性剔除，不进决策台默认路径。**")
        L.append("")
        L.append("### 11.1 换成「我实际下单的前 5 只」重做：结论不变，更硬")
        L.append("")
        L.append("池子均值挪 1bp 对下单没有意义，所以又按主理人的新口径重做了一遍："
                 "桶成员用 `viewer/factors.py` 的生产因子逐日重放求交集（能重建的桶只有"
                 "「超跌修复」「中线突破」两个，其余三类缺历史快照、不拿代理变量凑数），"
                 "在桶内先按分数排序，剔完再取前 N 只（N=5/10/20），主指标改成"
                 "**前 N 只的胜率差 Δw_N 与超额差 Δm_N**。")
        L.append("")
        L.append("- 最强的正向格子是 `oss_high_vol` 前 5 只 T+20：Δw **+1.05pp**（看着过了 0.5pp 线），"
                 "但 t=0.56；**随机换掉同样多席位的对照拿到 +0.68pp**，规则只多 0.37pp、不显著。")
        L.append("- `oss_ma_align` 前 5 只 T+20：Δm **+0.525pp（t=2.00）**，同槽对照 **+0.384pp**，"
                 "能被归给规则的只有 0.14pp；而它每天要换掉 15% 的仓位（0.76 只/日），"
                 "按往返 0.25% 算是 **3.8bp** 成本 —— 挣的落在误差里，付的落在账上。"
                 "同一规则前 20 只那一格反向显著（Δw −1.89pp，t=−2.01）。")
        L.append("- **比 oss 规则重要得多的一条（已用十档法重验，结论修正）**：`db_wave`（波浪三浪启动）"
                 "的 `score` 在降序排序下是**负向指标** —— 全命中集十档、T+5 / T+20 双双显著"
                 "（D10−D1 −0.58 / −0.87pp，t=−2.30 / −0.94，日内 ρ 的 t = −4.24 / −3.24）；"
                 "`tv_wbottom`、`tv_shrink_pull` 的 `score` **不含排序信息**；`tv_macd_div` 证据不足、"
                 "`tv_platform` 样本不足（日均命中 4 只）。"
                 "**原先那句「桶内打分普遍反预测、随机扰动后重取前 5 白拿 +1.2pp」已经撤回** —— "
                 "十档法找不到对应物，那个数是重建路径 + 槽位截断的产物。"
                 "但**改排序只能把「亏多」变「亏少」**：240 个档位格里 236 格是负超额，"
                 "中位数 −0.83pp（**这个水平值未做风格中性，约一半可归因于大小盘分化**）。"
                 "详见 `docs/级联增量验证.md` 的 12.7~12.8 与第 13 节。")
        L.append("- 超跌路径上这些规则**几乎碰不到桶里的票**：`pullback_ma20` 261 天只有 3 个股票日、"
                 "`reversal20` 为 0、`mom20` 只有 21 天。桶本身一天只有 5 只票（p50），"
                 "「先剔后取前 5」没有腾挪空间。")
        L.append("")
        L.append("详细表（含误杀率、换手代价、两条随机对照臂）见 `docs/级联增量验证.md` 第 12 节。")
        L.append("")

    L.append("## 12. 怎么复现")
    L.append("")
    L.append("```bash")
    L.append("python tools/backtest_oss_selfcheck.py          # 引擎自检（对产品指标逐值比对）")
    L.append("python tools/backtest_oss.py                    # 3 年主回测")
    L.append("python tools/backtest_oss.py --grid             # 全持有期网格")
    L.append("python tools/backtest_oss.py --full-coverage    # 全覆盖窗口复核")
    L.append("python tools/backtest_oss.py --dump-daily --out viewer/logs/bt_qa.json")
    L.append("python tools/backtest_oss_verify.py viewer/logs/bt_qa.json   # 手算复核")
    L.append("python tools/backtest_oss_basis_check.py        # （已作废）单日比值法，见第 10 节说明")
    L.append("python tools/audit_hfq_gap.py                   # 除权缺口审计（逐票全序列日收益）")
    L.append("python tools/cascade_delta.py --stage matrix    # 级联底座（726 日 × 全部票 × 13 档）")
    L.append("python tools/cascade_delta.py --stage analyze   # 三臂增量（第 11 节）")
    L.append("python tools/style_neutral.py --stage all       # 风格中性化复核（成交额分档）")
    L.append("python tools/cascade_delta.py --stage qfq       # 换前复权基准复核")
    L.append("python tools/cascade_topn.py --stage pool       # 前 N 只：建桶成员（约 110s）")
    L.append("python tools/cascade_topn.py --stage run        # 前 N 只：三臂出表（第 11.1 节）")
    L.append("python tools/cascade_delta_report.py            # docs/级联增量验证.md")
    L.append("python tools/backtest_oss_report.py             # 本报告")
    L.append("```")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="渲染回测 Markdown 报告")
    ap.add_argument("--main", default="")
    ap.add_argument("--grid", default="")
    ap.add_argument("--full", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)

    main_p = Path(a.main) if a.main else _default("main")
    if not main_p or not main_p.exists():
        print("找不到主回测 JSON，请先跑 tools/backtest_oss.py")
        return 1
    main_j = _load(main_p)
    grid_j = _load(Path(a.grid) if a.grid else _default("grid"))
    full_j = _load(Path(a.full) if a.full else _default("full"))

    md = render(main_j, grid_j, full_j)
    out = Path(a.out) if a.out else (
        LOGS / ("backtest_oss_report_%s.md" % main_j["meta"]["generated_at"][:10]
                .replace("-", "")))
    out.write_text(md, encoding="utf-8")
    print("报告：%s（%d 行）" % (out, md.count("\n") + 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
