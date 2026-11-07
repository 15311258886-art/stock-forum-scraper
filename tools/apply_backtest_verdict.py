# -*- coding: utf-8 -*-
"""T10 产出落地：把回测结论写回 `tool_registry` 的 bt_* 字段。

为什么必须有这一步
    回测跑完只是"知道了一件事"，产品行为一点没变 —— 这正是「工具货架」与「产品内核」
    的分界：结论不进 `evaluate()` 的判据，明天用户看到的还是同一套推荐。
    所以本脚本把 `bt_verdict / bt_edge / bt_hold / bt_at` 写回去，
    `tool_contract.static_evidence()` 会读它，`evaluate()` 立刻按新证据判资格。

判定规则（写死在这里，不许各处理器自己发挥）
    |t| ≥ 2 且 超额 < 0   → `validated_negative`（反向/排除专用，不得当正向选择器）
    |t| ≥ 2 且 超额 > 0   → `conditional`（**不直接给 positive**：单一区间单一样本，
                            必须另有样本外/分段时间稳定性支持才够格升 positive）
    |t| < 2               → `unvalidated`（方向不明，只能观察，不能当 ACTIONABLE 的唯一依据）
    入选率 < 0.5%         → 额外标注「样本过少，结论不可用」，不写 verdict

安全
    默认 **dry-run，只打印将要改什么**。加 `--write` 才落库，并把改动前后写进
    `viewer/logs/backtest/verdict_write_*.json`，便于回滚与事后追责。

用法
    python tools/apply_backtest_verdict.py viewer/logs/backtest/router_backtest_kline_T20_*.json
    python tools/apply_backtest_verdict.py <json> --write
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

APP_DB = os.path.join(ROOT, "viewer", "app.db")


def decide(row: dict, min_sections: int = 100) -> tuple[str, str]:
    """→ (verdict, 中文理由)。verdict 形如 validated_negative / conditional / unvalidated / skip。

    ★ 样本量门槛（`min_sections`，默认 100 个截面 ≈ 2 年按周采样）：
      没到这个量的回测只能当**参考**，不能改判据。理由是统计学上的常识 ——
      13 个月 45 个截面的 t 值，与 14.8 年 103 万样本的结论冲突时，
      该被质疑的是前者，不是后者。所以这类行返回 `skip` + 说明，
      让它出现在报告里，但不进库。
    """
    if not row.get("ok"):
        return "skip", "回测未成功：%s" % str(row.get("error") or "")[:60]
    o = row.get("overall") or {}
    ex, t, pct = o.get("excess_pp"), o.get("t"), o.get("pct_kept")
    n = o.get("sections", 0)
    if ex is None or n < 10:
        return "skip", "截面不足（%s 个），不下结论" % n
    if pct is not None and pct < 0.5:
        return "skip", "入选率仅 %.2f%%，样本过少，结论不可用" % pct
    if n < min_sections:
        return "skip", ("仅 %d 个截面（< %d），样本期太短 —— 只作参考，"
                        "不足以改判据（短样本不能推翻长样本）" % (n, min_sections))
    if t is None:
        return "unvalidated", "样本波动无法估计 t 值，按未验证处理"

    # ★ 分段一致性守卫（2026-11-06 补，踩过才加的）：
    #   全期 t 只是「平均」，两段大负 + 四段小正 也能拼出 t=-2.28。
    #   这种「方向不稳」的被判成 validated_negative，等于把噪声当结论。
    #   `tv_shrink_pull` 正是这一例（-0.61pp / t=-2.28，但 6 段正负交替）。
    #   门槛取 0.7：6 段里至少 5 段同向（83%）才算稳定，4/6（67%）不算。
    segs = [s for s in (row.get("by_segment") or []) if s.get("excess_pp") is not None]
    if len(segs) >= 4:
        neg = sum(1 for s in segs if s["excess_pp"] < 0)
        same = neg if ex < 0 else (len(segs) - neg)
        ratio = same / len(segs)
        if ratio < 0.7:
            return "skip", ("全期 %+.2fpp / t=%.2f，但分段只有 %d/%d 段同向（%.0f%% < 70%%）—— "
                            "方向不稳定，是平均出来的结论，不改判据"
                            % (ex, t, same, len(segs), ratio * 100))

    if abs(t) >= 2.0:
        if ex < 0:
            # ★ 双口径守卫（2026-11-06 补，中性化跑出来才发现的漏洞）：
            #   全池超额显著为负，不代表工具本身反向 —— 也可能是它专挑某个规模档，
            #   而那一段该档整体跑输。db_wave 就是这一例：全池 -0.61pp/t=-2.28，
            #   剔除规模风格后只剩 -0.16pp/t=-0.63，分段仅 2/6 为负。
            #   判 validated_negative 必须有「同规模档也显著」这一半证据。
            tliq, exliq = o.get("t_liq"), o.get("excess_pp_liq")
            if tliq is not None and abs(tliq) < 2.0:
                return "skip", ("全池 %+.2fpp t=%.2f 显著，但同规模档 %+.2fpp t=%.2f 不显著 —— "
                                "负超额主要来自规模错配，不是工具本身失效，不改判据"
                                % (ex, t, exliq if exliq is not None else 0.0, tliq))
            return "validated_negative", ("%+.2fpp，t=%.2f（|t|≥2）%s：反向显著 —— "
                                          "只能用作排除/反向，不得当正向选择器"
                                          % (ex, t, "，同规模档 %+.2fpp t=%.2f" % (exliq, tliq)
                                             if tliq is not None else ""))
        return "conditional", ("%+.2fpp，t=%.2f（|t|≥2）：正向显著但仅单区间单样本，"
                               "升 positive 需样本外验证" % (ex, t))
    return "unvalidated", ("%+.2fpp，t=%.2f（|t|<2）：方向不显著，只能观察，"
                           "不得作为 ACTIONABLE 的唯一依据" % (ex, t))


# 旧 verdict 的强弱序（数字越大越"有结论"）。
# 用途：**新结论不比旧的强时不许覆盖** —— 13 个月的不显著，不能把 14.8 年的
# "反向显著"擦成"未验证"。这条是这套落地脚本最容易被误用的地方。
_STRENGTH = {"validated_negative": 3, "validated_positive": 3,
             "conditional": 2, "unvalidated": 1, "": 0}
_OLD_VERDICT_MAP = {"A": "validated_positive", "B": "validated_negative",
                    "C": "conditional", "D": "unvalidated"}

# 落库时写**中文标签**，与库里既有 12 条保持一致（`tool_contract.VERDICT_MAP` 认的就是它）。
# 直接写英文枚举是错的：`normalize_verdict("validated_negative")` 会被读成 unvalidated，
# 于是「写入成功、读取无效」—— 查库有值，判定照旧。2026-11-06 踩过一次。
_VERDICT_LABEL = {"validated_negative": "B 否决·反向",
                  "validated_positive": "A 采纳",
                  "conditional": "C 有条件采纳",
                  # 降级回「无裁决」时清空该字段：空串在 VERDICT_MAP 里就是 unvalidated，
                  # 比写一个英文枚举更干净（页面上不会出现看不懂的字符串）。
                  # 降级前的旧值留在 verdict_write_*.json 的 before 里，可追溯。
                  "unvalidated": ""}


def _norm_old(old: str) -> str:
    """把库里 `B 否决·反向` 这类中文标签归一成枚举值。"""
    s = str(old or "").strip()
    if not s:
        return ""
    for k, v in _OLD_VERDICT_MAP.items():
        if s.upper().startswith(k):
            return v
    for k in _STRENGTH:
        if k and k in s:
            return k
    return ""


def _weaker_or_conflicting(old: str, new: str) -> bool:
    """新结论是不是"更弱"或"反向推翻"了旧结论 → 不许覆盖。"""
    o = _norm_old(old)
    if not o:
        return False
    if new == "unvalidated" and o in ("validated_negative", "validated_positive"):
        return True
    if _STRENGTH.get(new, 0) < _STRENGTH.get(o, 0) and o != "unvalidated":
        return True
    if {o, new} == {"validated_negative", "validated_positive"}:
        return True
    return False


def _reg_key(tool: str) -> str:
    """回测引擎的工具名 → `tool_registry.tool_key`。

    回测里 `factor:` 只是「按因子接口调用」的分类标记（`factor:db_wave`），
    注册表里的键没有前缀（`db_wave`）。不归一的话 `UPDATE ... WHERE tool_key=?`
    会命中 0 行 —— 而且 **SQLite 不会报错**，脚本照样打印「已写入 3 个工具」。
    这个坑是 2026-11-06 第一次 --write 时踩到的，故把归一放成唯一入口。
    """
    s = str(tool or "").strip()
    for pre in ("factor:", "tool:", "screen:"):
        if s.startswith(pre):
            return s[len(pre):]
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="回测结果 JSON（支持通配）")
    ap.add_argument("--write", action="store_true", help="真正写库（默认只 dry-run）")
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--allow-downgrade", action="store_true",
                    help="允许用更强证据把旧结论降级（例如中性化证明原「反向」只是风格错配）。"
                         "默认禁止降级，避免短样本把长样本擦掉")
    a = ap.parse_args(argv)

    paths = []
    for f in a.files:
        paths.extend(sorted(glob.glob(f)) or [f])

    rows = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        # 两种格式都要吃：
        #   ① 批量（router_backtest_all）→ {"rows": [每个工具一条]}
        #   ② 单工具（router_backtest --out）→ 结果本身就是一条
        batch = d.get("rows") or ([d] if d.get("tool") and d.get("overall") else [])
        for r in batch:
            r.setdefault("horizon", d.get("horizon")
                         or (d.get("params") or {}).get("horizon") or a.horizon)
            r["_src"] = os.path.basename(p)
            rows.append(r)

    if not rows:
        print("没有读到任何回测行")
        return 1

    con = sqlite3.connect("file:%s?mode=ro" % APP_DB.replace("\\", "/"), uri=True)
    try:
        cur_bt = {}
        exists = set()
        for k, v, e in con.execute("SELECT tool_key, bt_verdict, bt_edge FROM tool_registry"):
            cur_bt[str(k)] = (str(v or ""), e)
            exists.add(str(k))
    finally:
        con.close()

    plan, changes = [], []
    for r in rows:
        key = _reg_key(r["tool"])
        v, why = decide(r)
        o = r.get("overall") or {}
        old, old_edge = cur_bt.get(key, ("", None))
        if v not in ("skip", "") and _weaker_or_conflicting(old, v) and not a.allow_downgrade:
            v = "skip"
            why = ("新结论（%s）弱于或反向于现有结论（%s）—— 长样本优先，保留原判。%s"
                   % (v, old, why))
        # 中性化推翻旧判：旧结论是「反向显著」，但同规模档复核不显著 →
        # 这不是「样本变弱了」，而是**旧结论的归因错了**（把风格错配当成工具失效）。
        # 只有显式 --allow-downgrade 才允许走这条路，且理由里必须带「规模错配」。
        if (a.allow_downgrade and v == "skip" and "规模错配" in why
                and _norm_old(old) == "validated_negative"):
            v = "unvalidated"
            why = "中性化复核推翻原判：" + why
        plan.append((key, old or "（空）", v, o.get("excess_pp"), o.get("t"), why))
        if v != "skip" and v != _norm_old(old):
            # `regime` 取回测行的市况分层；本批是全样本混合（不分行情），故落 "全部"。
            # 不写空串：「全部」和「未知」在页面上是两句话，别让它们长得一样。
            regime = str(r.get("market_regime") or r.get("regime") or "全部")
            changes.append((key, v, regime, o.get("excess_pp"), r["horizon"], why))

    print("%-24s %-20s %-20s %10s %8s  %s" % ("工具", "原 verdict", "建议", "超额pp", "t", "理由"))
    print("-" * 130)
    for key, old, v, ex, t, why in plan:
        print("%-24s %-20s %-20s %10s %8s  %s" % (
            key, old, v, "—" if ex is None else "%+.2f" % ex,
            "—" if t is None else "%.2f" % t, why))

    print("\n共 %d 个工具，其中 %d 个 verdict 需要变。" % (len(plan), len(changes)))
    if not changes:
        return 0

    # ★ fail-closed：目标键不存在就一个字都不写。
    #   2026-11-06 之前的坑：`UPDATE` 命中 0 行不报错，脚本仍打印「已写入 3 个工具」，
    #   而库里的 bt_verdict 还是空的 —— 等于假报工。现在宁可整批失败。
    missing = [c[0] for c in changes if c[0] not in exists]
    if missing:
        print("⚠ 以下工具在 tool_registry 中不存在，整批拒绝写入：%s" % ", ".join(missing))
        print("  （检查回测 JSON 的工具名与注册表键名是否一致）")
        return 2

    if not a.write:
        print("（dry-run：未写库。确认无误后加 --write）")
        return 0

    # ---- 落库：只动 bt_* 四个字段，不碰 status / default_params
    outdir = os.path.join(ROOT, "viewer", "logs", "backtest")
    os.makedirs(outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    logp = os.path.join(outdir, "verdict_write_%s.json" % stamp)
    con = sqlite3.connect(APP_DB)
    try:
        before = {}
        zero = []
        for key, v, regime, ex, hz, why in changes:
            row = con.execute("SELECT bt_verdict, bt_edge, bt_hold, bt_at FROM tool_registry"
                              " WHERE tool_key=?", (key,)).fetchone()
            before[key] = row
            cur = con.execute(
                "UPDATE tool_registry SET bt_verdict=?, bt_regime=?, bt_edge=?, bt_hold=?, bt_at=?"
                " WHERE tool_key=?",
                (_VERDICT_LABEL.get(v, v), regime, ex, int(hz), stamp, key))
            if cur.rowcount == 0:
                zero.append(key)
        if zero:
            con.rollback()                      # 双保险：一行没命中就整批回滚
            print("⚠ UPDATE 未命中：%s —— 已回滚，未改动任何数据" % ", ".join(zero))
            return 2
        con.commit()
    finally:
        con.close()
    with open(logp, "w", encoding="utf-8") as f:
        json.dump({"at": stamp, "before": {k: list(v or []) for k, v in before.items()},
                   "after": {k: {"verdict": v, "regime": rg, "edge": ex,
                                 "hold": int(hz), "why": why}
                             for k, v, rg, ex, hz, why in changes}},
                  f, ensure_ascii=False, indent=1)
    print("已写入 %d 个工具，改动前后 → %s" % (len(changes), logp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
