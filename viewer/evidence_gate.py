# -*- coding: utf-8 -*-
"""证据硬门禁 —— 「这个信号配不配叫机会」的**唯一**裁决处（2026-11-01，测评 P0-2）。

## 为什么需要这一层（问题不在缺数据，在结论没有出口）

本项目已经有一份相当严格的实证账本 `viewer/evidence.py`，里面白纸黑字记着：

  · `lhb_turnover`（龙虎榜高换手）T+5 超额 **−0.91%**、t=−11.28、4077 个交易日
  · `forum_hold`（论坛实盘选手 HOLD）−1.96%、t=−1.79 → **refuted**
  · `selfpick_a`（每日荐股 A 档）−3.00%、t=−2.05 → watch
  · `forum_bullish`（淘股吧看多帖）−1.41%、t=−2.02 → watch

而首页「今日决策」的 **② 今日机会 · 能上车的** 卡片，主要来源恰恰是**龙虎榜梯队里
尚未涨停的同方向票**（`_today_decision_compute` 从 `sector_chain.tiers_json` 取）。

于是产品在做一件自相矛盾的事：**用一条 T+5 显著为负的信号，给用户打上「能上车的」标签。**
账本知道结论是负的，界面不知道 —— 因为从「账本」到「界面」之间，没有任何一处代码
把两边接起来。缺的不是数据，也不是判定，是**一个必须经过的关口**。

本文件就是那个关口，也是**唯一允许**把「信号来源」映射成「证据裁决」的地方。
别处（server、app.js、别的模块）一律不许再写 `if status == "refuted"` 之类的判断 ——
那种写法一旦散落三处，就一定会分叉，而分叉的后果是「同一个信号在两个面板里
一个能用一个不能用」，用户没有任何办法判断该信哪个。

## 三档裁决（只有三档，不要自造第四档）

  executable  可进入「今日可执行机会」—— 有 claim、方向为正、显著、且**扣费后还剩钱**
  watch       只能进「观察/待验证」—— 无实证 / 证据不足 / 幅度不抵成本
  blocked     完全不显示 —— 已证伪，或命中**显著为负**的实证

## 三条判定纪律（都是从账本的方法论直接搬过来的）

1. **无实证 ≠ 可用。** 没登记 claim 的来源一律 watch。默认值是 watch 不是 executable ——
   这个默认值的方向是有讲究的：漏放一条好信号只是少赚，放进去一条负信号是让用户亏钱。
2. **显著 ≠ 可用。** |t|≥2 只说明不是噪声；能否进「可执行」还要看幅度。
   幅度门槛直接取 `viewer/hypo.py` 的 `MIN_EFFECT`（0.30%），不另立一个数。
3. **方向为负的 active 不是「可用」，是「确定别用」。** 账本里 status=active 只表示
   「统计上不是噪声」，不表示「方向对」。`lhb_turnover` 就是这种：active 且 excess<0
   且 |t|≥2 —— 这是**负面实证**，比 refuted 更硬（refuted 是「推翻」，这是「确证有害」）。

## 关于 net / mdd / invalidate 三个字段

  · `net`（扣费后）：账本的 excess 是**毛超额**（`hypo.py` 口径：个股收益 − 同日全市场
    等权，不扣成本），所以这里减掉**回测口径双边成本**，数字取自
    `viewer/kernel/params.BACKTEST_FEE_ROUNDTRIP`（15bp = 0.15%），不写死字面量。
  · `mdd`（最大回撤）：**账本里没有这个字段，所以这里一律返回 None。**
    绝不拿「胜率」冒充回撤，也绝不用日收益标准差估算 —— 估出来的回撤比没有回撤更危险，
    因为它看起来像个测量值。前端拿到 None 显示「—」，label 里如实写「回撤未测」。
    补 MDD 是下一轮工作的输入（见 `viewer/evidence.py` 顶部的 TODO）。
  · `invalidate`（失效条件）：按来源给固定文案，写死在 `SOURCES` 里，必须可判定
    （「净流入转负」可判定，「情绪转弱」不可判定，后者不许写）。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 裁决三档
TIER_EXECUTABLE = "executable"
TIER_WATCH = "watch"
TIER_BLOCKED = "blocked"

#: 严重度排序：一个来源挂多条 claim 时取**最严重**的那条（宁可少放，不可错放）。
_TIER_RANK = {TIER_BLOCKED: 0, TIER_WATCH: 1, TIER_EXECUTABLE: 2}

#: 账本状态 → 前端 level（枚举固定这五个，别处不要再发明）
LEVEL_NO_EVIDENCE = "no_evidence"


# ---------------------------------------------------------------- 外部依赖（都可降级）
def _ledger():
    """拿实证账本模块。两种导入方式都要能用：

    `python -m viewer.server` 时走 `from viewer import evidence`；
    以 `viewer/` 为工作目录直接跑脚本时 `import evidence`。
    两种都失败就让账本等于空 —— 那时所有来源都会落到 watch（无实证），
    这是**安全的降级方向**：丢功能，不丢正确性。
    """
    try:
        from viewer import evidence as _ev  # noqa: F401
        return _ev
    except Exception:  # noqa: BLE001
        try:
            import evidence as _ev2  # noqa: F401
            return _ev2
        except Exception:  # noqa: BLE001
            return None


_LEDGER = None
_LEDGER_TRIED = False


def _get_ledger():
    global _LEDGER, _LEDGER_TRIED
    if not _LEDGER_TRIED:
        _LEDGER = _ledger()
        _LEDGER_TRIED = True
    return _LEDGER


def claim_of(key: str):
    """按 key 取账本里的 claim（副本）。账本缺失或没这条 → None。"""
    ev = _get_ledger()
    if ev is None or not key:
        return None
    try:
        c = ev.get(key)
    except Exception:  # noqa: BLE001
        return None
    return dict(c) if c else None


def _cost_pct() -> float:
    """回测口径双边成本（百分数）。15bp → 0.15。

    刻意**不写 0.2**：成本口径在 `viewer/kernel/params.py` 有唯一来源，
    这里再写一个字面量就又是一处「两套成本」，也就是 `cost_basis.py` 文件头
    专门警告过的那种病。
    """
    try:
        from viewer.kernel import params as _P
        return float(_P.BACKTEST_FEE_ROUNDTRIP) * 100.0
    except Exception:  # noqa: BLE001
        try:
            from kernel import params as _P2  # 以 viewer/ 为工作目录时的退化路径
            return float(_P2.BACKTEST_FEE_ROUNDTRIP) * 100.0
        except Exception:  # noqa: BLE001
            return 0.15


def _min_effect() -> float:
    """最小有效幅度（%）。直接取 `hypo.py` 的 MIN_EFFECT，不另立阈值。"""
    try:
        from viewer.hypo import MIN_EFFECT as _M
        return float(_M)
    except Exception:  # noqa: BLE001
        try:
            from hypo import MIN_EFFECT as _M2
            return float(_M2)
        except Exception:  # noqa: BLE001
            return 0.30


# ---------------------------------------------------------------- 格式化小工具
def _pct(v) -> str:
    """-0.91 → "-0.91%"；正数带 +。"""
    try:
        return "%+.2f%%" % float(v)
    except (TypeError, ValueError):
        return "—"


def _tstr(v) -> str:
    try:
        return "%+.2f" % float(v)
    except (TypeError, ValueError):
        return "—"


def _fmt_int(n) -> str:
    """4077 → "4077"（不千分位：这是交易日数，读者不需要分组）。"""
    try:
        return "%d" % int(n)
    except (TypeError, ValueError):
        return "—"


def _fmt_samples(n) -> str:
    """372962 → "37.3 万"；127 → "127 个"。"""
    try:
        i = int(n)
    except (TypeError, ValueError):
        return "—"
    if i >= 10000:
        return "%.1f 万" % (i / 10000.0)
    return "%d 个" % i


def _fmt_sample(c) -> str:
    """样本量的人话写法：优先给「几个交易日」（它比 n 更能说明可信度），再给事件数。"""
    c = c or {}
    parts = []
    if c.get("days"):
        parts.append("%s 个交易日" % _fmt_int(c.get("days")))
    if c.get("n"):
        parts.append("%s样本" % _fmt_samples(c.get("n")))
    return " / ".join(parts) if parts else "—"


def _fmt_excess(c) -> str:
    """T+5 超额 -0.91%（账本里的主数字）。"""
    c = c or {}
    if c.get("excess") is None:
        return "—"
    try:
        h = int(c.get("horizon"))
    except (TypeError, ValueError):
        h = 5
    return "T+%d 超额 %s" % (h, _pct(c.get("excess")))


def _net_of(c):
    """扣费后收益（%）。毛超额 − 回测口径双边成本。无 excess → None。"""
    c = c or {}
    ex = c.get("excess")
    if ex is None:
        return None
    try:
        return round(float(ex) - _cost_pct(), 2)
    except (TypeError, ValueError):
        return None


def _mdd_of(c):
    """最大回撤（%）。账本**没有**这个字段 → None。

    不估算、不用胜率顶替。见文件头说明。
    """
    c = c or {}
    for k in ("mdd", "max_dd"):
        v = c.get(k)
        if v is not None:
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------- 来源登记表
#
# 这是全项目**唯一**「信号来源 → 实证结论」的映射点。新增一个会往首页塞票的来源时，
# 必须在这里先登记；不登记也能跑，但会被判 watch 并在界面上写「未纳入证据账本」——
# 这是刻意留的一道难看痕迹：让「偷偷加了个没实证的来源」这件事在界面上自己暴露。
#
# 字段：
#   name        人话名字（空态文案直接用它起头）
#   claims      关联的 claim key（可以多条；取最严重的那条）
#   invalidate  失效条件（必须可判定，不许写「情绪转弱」这种没法核对的）
#   note        这条来源为什么这么定档（给维护者看，也进 reason）
#   label_suffix 徽章后缀（仅方向卡这种「无实证但要标明出处」的场景用）
SOURCES: dict = {
    # ---- 主线龙虎榜梯队：首页机会卡的第一大来源，也是本轮 P0-2 的正主 ----
    "mainline_tier": {
        "name": "主线龙虎榜梯队的「可推进」票",
        "claims": ("lhb_turnover",),
        "invalidate": "次日板块净流入转负，或龙头跌破当日低点即失效",
        "note": ("这条来源的实证依据就是「龙虎榜高换手」。账本给的 T+5 是负的，"
                 "所以它不是「可执行机会」的合格来源 —— 判定完全按账本数字算，"
                 "账本哪天翻正，这里自动跟着变。"),
    },
    # ---- 四支柱补位：**没有**任何 claim 覆盖 → 无实证 → watch ----
    "pillar_policy_funnel": {
        "name": "四支柱·政策漏斗",
        "claims": (),
        "invalidate": "次日竞价量能低于昨日 70% 即失效",
        "note": "政策漏斗是规则筛选，没有独立的前向超额实证，只能当线索。",
    },
    "pillar_dip_revert": {
        "name": "四支柱·超跌修复",
        "claims": (),
        "invalidate": "次日收盘跌破入选日最低价即失效",
        "note": "超跌修复依赖「跌多了会弹」，而账本里「大跌」T+5 是 −1.48%（t=−11.71），"
                "反向证据比正向证据更硬 —— 在拿到它自己的超额实证之前，不许当机会。",
    },
    "pillar_t1_surf": {
        "name": "四支柱·竞价冲浪",
        "claims": (),
        "invalidate": "次日竞价量能低于昨日 70% 即失效",
        "note": "竞价冲浪只有当日强度、没有 T+N 超额实证。",
    },
    "pillar_resonance": {
        "name": "四支柱·博主共振",
        "claims": (),
        "invalidate": "共振票次日跌出板块涨幅前 50% 即失效",
        "note": "共振是「多人同时看好」，而账本里 forum_bullish（看多帖）T+5 −1.41%、"
                "forum_hold −1.96% 都指向「社区热度不是买入理由」。",
    },
    # ---- 每日荐股 A 档：账本里是 watch（只有 6 个独立交易日）→ watch ----
    "selfpick_a": {
        "name": "每日荐股 A 档",
        "claims": ("selfpick_a",),
        "invalidate": "累计 60 个交易日后重算 T+5 超额仍为负，即永久下线该档",
        "note": "6 个独立交易日的 t 值不可靠，账本自己标了 watch；等样本够了再评。",
    },
    # ---- 论坛类：已停抓，账本留档防人重新捡起 ----
    "forum_hold": {
        "name": "论坛实盘选手持仓（HOLD）",
        "claims": ("forum_hold",),
        "invalidate": "配对口径（同日、前 5 日涨幅差 ≤2pp）下 T+5 超额转正即恢复观察",
        "note": "配对后 −1.96%（t=−1.79）不显著，账本判 refuted：原始 −3.08% 里有一大半"
                "只是「前期涨得差不多的股票也会跌」。",
    },
    "forum_bullish": {
        "name": "淘股吧看多帖",
        "claims": ("forum_bullish",),
        "invalidate": "源恢复采集并累计 ≥60 个交易日、T+5 超额转正即重评",
        "note": "t 值刚过线但源已停更（Cookie 失效），无法继续验证 → 只观察。",
    },
    # ---- 方向卡：板块资金流，无 claim → 无实证（但要标明出处，不许显示成正面） ----
    "sector_flow": {
        "name": "板块资金流（sector_chain 主线研判）",
        "claims": (),
        "invalidate": "次日该板块资金转为净流出即失效",
        "note": "方向来自交易所龙虎榜的资金聚合，是「发生了什么」；"
                "账本里没有「板块净流入 → T+N 超额」这条 claim，所以只有事实、没有实证。",
        "label_suffix": "（板块资金流）",
    },
}

#: 未登记来源统一用的失效条件（写死一句可判定的兜底，不留空）
_DEFAULT_INVALIDATE = "未登记来源无失效条件可判 —— 拿到实证 claim 之前不许当机会"


# ---------------------------------------------------------------- 判定引擎
def _reason_for(claim: dict, kind: str) -> str:
    """把 (claim, 判定种类) 翻成一句能直接上屏的人话。数字全部来自 claim 本身。"""
    c = claim or {}
    name = c.get("name") or c.get("key") or "（未命名实证）"
    ex = c.get("excess")
    t = c.get("t")
    smp = _fmt_sample(c)
    net = _net_of(c)
    h = c.get("horizon") or 5

    if kind == "refuted":
        return ("「%s」实测 T+%s 超额 %s（t=%s）已被证伪 —— 已证伪的结论禁止在任何判断里使用。"
                % (name, h, _pct(ex), _tstr(t)))
    if kind == "negative":
        return ("「%s」T+%s 超额 %s（t=%s，%s）为负，属负面实证 —— "
                "负面实证不得当「能上车」的机会。" % (name, h, _pct(ex), _tstr(t), smp))
    if kind == "positive":
        return ("「%s」T+%s 超额 %s（t=%s，%s）显著为正，扣掉双边成本 %s 后仍有 %s。"
                % (name, h, _pct(ex), _tstr(t), smp, _pct(_cost_pct()), _pct(net)))
    if kind == "marginal":
        return ("「%s」T+%s 超额 %s（t=%s）方向为正，但扣掉双边成本 %s 后只剩 %s，"
                "幅度不够支撑一次操作。" % (name, h, _pct(ex), _tstr(t),
                                            _pct(_cost_pct()), _pct(net)))
    if kind == "not_significant":
        return ("「%s」T+%s 超额 %s 的 t=%s（|t|<2），统计上还分不清是信号还是噪声。"
                % (name, h, _pct(ex), _tstr(t)))
    if kind == "no_number":
        return "「%s」在账本里只有状态、没有超额数字 —— 无法判定，按证据不足处理。" % name
    if kind == "weak":
        return ("「%s」只有 %s（weak，强度不足）—— 只能当提示，不能当依据。"
                % (name, smp))
    if kind == "watch":
        return ("「%s」只有 %s（watch，样本太少或证据互相矛盾）—— 只观察，不进规则。"
                % (name, smp))
    return "「%s」的证据状态无法识别，按证据不足处理。" % name


def _judge_one(claim: dict) -> dict:
    """单条 claim → {tier, level, kind, reason}。

    判定顺序刻意「先否决」：先判 refuted / 负面实证，再判可用。
    反过来的写法（先判可用）容易在边界上把负面实证漏进可执行。
    """
    c = claim or {}
    st = str(c.get("status") or "").strip()
    ex = c.get("excess")
    t = c.get("t")

    if st == "refuted":
        return {"tier": TIER_BLOCKED, "level": "refuted", "kind": "refuted",
                "reason": _reason_for(c, "refuted")}

    if st == "active":
        if ex is None or t is None:
            return {"tier": TIER_WATCH, "level": "active", "kind": "no_number",
                    "reason": _reason_for(c, "no_number")}
        try:
            exf = float(ex)
            tf = float(t)
        except (TypeError, ValueError):
            return {"tier": TIER_WATCH, "level": "active", "kind": "no_number",
                    "reason": _reason_for(c, "no_number")}
        significant = abs(tf) >= 2.0
        if significant and exf < 0:
            # 这是本文件存在的核心理由：active 不等于方向对。
            return {"tier": TIER_BLOCKED, "level": "active", "kind": "negative",
                    "reason": _reason_for(c, "negative")}
        if not significant:
            return {"tier": TIER_WATCH, "level": "active", "kind": "not_significant",
                    "reason": _reason_for(c, "not_significant")}
        net = _net_of(c)
        if net is not None and net > 0 and abs(exf) >= _min_effect():
            return {"tier": TIER_EXECUTABLE, "level": "active", "kind": "positive",
                    "reason": _reason_for(c, "positive")}
        return {"tier": TIER_WATCH, "level": "active", "kind": "marginal",
                "reason": _reason_for(c, "marginal")}

    if st == "weak":
        return {"tier": TIER_WATCH, "level": "weak", "kind": "weak",
                "reason": _reason_for(c, "weak")}
    if st == "watch":
        return {"tier": TIER_WATCH, "level": "watch", "kind": "watch",
                "reason": _reason_for(c, "watch")}
    return {"tier": TIER_WATCH, "level": LEVEL_NO_EVIDENCE, "kind": "no_number",
            "reason": _reason_for(c, "no_number")}


def _worst(verdicts: list) -> dict:
    """多条 claim 取最严重的一条（blocked > watch > executable）。"""
    if not verdicts:
        return {}
    return sorted(verdicts, key=lambda v: _TIER_RANK.get(v.get("tier"), 1))[0]


def _label_of(tier: str, level: str, mdd, has_claim: bool = True) -> str:
    """徽章上的标签。

    缺 MDD 时如实追加「回撤未测」—— 但**只在真有 claim 的时候**才加：
    四支柱/板块资金流这类无实证来源本来就没有任何实证数字，写「回撤未测」是废话，
    反倒把「无实证」这个更要紧的信息盖住了。
    """
    if tier == TIER_BLOCKED:
        base = "已证伪" if level == "refuted" else "负面实证"
    elif tier == TIER_EXECUTABLE:
        base = "可用实证"
    else:
        base = "无实证" if level == LEVEL_NO_EVIDENCE else "证据不足"
    if not has_claim or level == LEVEL_NO_EVIDENCE:
        return base
    return base if mdd is not None else base + " · 回撤未测"


def evaluate(source_key: str) -> dict:
    """来源 → 裁决。**未登记的来源一律 watch**，绝不默认 executable。

    返回（字段固定，前端与测试都依赖）：
      source  str      传入的 source_key
      name    str      人话名字
      tier    str      executable / watch / blocked
      status  str      主导 claim 的账本状态，或 "no_evidence"
      claims  list     关联 claim 的**副本**（含 key/name/status/excess/t/n/days/horizon/win）
      reason  str      可直接上屏的人话理由（数字来自 claim）
      invalidate str   失效条件（可判定）
      level   str      active / weak / watch / refuted / no_evidence
      label   str      徽章标签
      label_short str  标签 + 出处后缀（方向卡用）
    """
    key = str(source_key or "")
    src = SOURCES.get(key)
    if not src:
        _lbl = _label_of(TIER_WATCH, LEVEL_NO_EVIDENCE, None, has_claim=False)
        return {
            "source": key, "name": key or "（空来源）",
            "tier": TIER_WATCH, "status": LEVEL_NO_EVIDENCE, "claims": [],
            "reason": "「%s」未纳入证据账本 —— 没有实证支撑的来源只做观察，不做可执行。"
                      % (key or "（空来源）"),
            "invalidate": _DEFAULT_INVALIDATE,
            "level": LEVEL_NO_EVIDENCE, "label": _lbl, "label_short": _lbl,
        }

    name = str(src.get("name") or key)
    claims = [c for c in (claim_of(k) for k in (src.get("claims") or ())) if c]

    if not claims:
        # 无 claim：事实可以有，实证没有 → 只能观察。
        _lbl = _label_of(TIER_WATCH, LEVEL_NO_EVIDENCE, None, has_claim=False)
        return {
            "source": key, "name": name,
            "tier": TIER_WATCH, "status": LEVEL_NO_EVIDENCE, "claims": [],
            "reason": ("「%s」没有对应的实证 claim —— 无实证不等于可用，"
                       "只能进「观察/待验证」。" % name),
            "invalidate": str(src.get("invalidate") or _DEFAULT_INVALIDATE),
            "level": LEVEL_NO_EVIDENCE, "label": _lbl,
            "label_short": _lbl + str(src.get("label_suffix") or ""),
        }

    verdicts = [_judge_one(c) for c in claims]
    worst = _worst(verdicts)
    tier = worst.get("tier", TIER_WATCH)
    level = worst.get("level", LEVEL_NO_EVIDENCE)
    reason = worst.get("reason") or "证据不足。"

    # 严重档位被更轻的 claim 稀释时补一句：让读者知道「这条来源里还有别的实证」。
    if len(claims) > 1 and len({v.get("tier") for v in verdicts}) > 1:
        reason += "（该来源关联 %d 条实证，已按最严重的一条定档）" % len(claims)

    mdd = _mdd_of(claims[0])
    label = _label_of(tier, level, mdd)
    return {
        "source": key, "name": name,
        "tier": tier, "status": str(claims[0].get("status") or LEVEL_NO_EVIDENCE),
        "claims": claims,
        "reason": reason,
        "invalidate": str(src.get("invalidate") or _DEFAULT_INVALIDATE),
        "level": level,
        "label": label,
        "label_short": label + str(src.get("label_suffix") or ""),
    }


def annotate(item: dict, source_key: str) -> dict:
    """给一条机会行挂上 `evidence` 徽章数据。**返回副本**，不改调用方传进来的对象。

    为什么返回副本：同一个 item 可能同时被两处（机会卡/观察区）引用，
    原地改会让「谁在什么时候改的」无法追溯。
    """
    out = dict(item or {})
    ev = evaluate(source_key)
    claim = ev["claims"][0] if ev.get("claims") else None
    mdd = _mdd_of(claim)
    out["source"] = ev["source"]          # 审计用：这一行凭哪条来源进来的
    out["evidence"] = {
        "tier": ev["tier"],
        "level": ev["level"],
        "label": ev["label"],
        "label_short": ev["label_short"],
        "source": ev["source"],
        "source_name": ev["name"],
        "claim": (claim or {}).get("key"),
        "claim_name": (claim or {}).get("name"),
        "sample": _fmt_sample(claim),
        "excess": _fmt_excess(claim),
        "excess_pct": (claim or {}).get("excess"),
        "t": (claim or {}).get("t"),
        "net": _net_of(claim),          # 扣费后（%）；None = 没有 claim
        "mdd": mdd,                     # 账本没有 MDD 字段 → 恒为 None（不估算）
        "invalidate": ev["invalidate"],
        "reason": ev["reason"],
    }
    return out


def summary(items=None) -> dict:
    """给首页一行状态用。

    `items` 不传 → 按**来源**维度统计（纯函数，可单测、不依赖运行时）。
    `items` 传（一组已经 `annotate` 过的机会行）→ 按**条数**维度统计，
    因为首页要显示的是「已屏蔽 N 条」，条数只有运行时知道。

    返回：
      executable / watch / blocked  计数（单位见 count_unit）
      blocked_sources   list[str]   被判 blocked 的 source_key
      blocked_details   list[dict]  同上但带 name/reason/claim，空态文案直接用
      count_unit        "items" | "sources"
    """
    by_tier: dict = {TIER_EXECUTABLE: [], TIER_WATCH: [], TIER_BLOCKED: []}
    details: list = []
    for key in SOURCES:
        ev = evaluate(key)
        by_tier.setdefault(ev["tier"], []).append(key)
        if ev["tier"] == TIER_BLOCKED:
            c0 = ev["claims"][0] if ev["claims"] else {}
            details.append({
                "source": key, "name": ev["name"], "reason": ev["reason"],
                "claim": c0.get("key"), "claim_name": c0.get("name"),
                "excess": c0.get("excess"), "t": c0.get("t"),
                "horizon": c0.get("horizon"), "sample": _fmt_sample(c0),
            })

    out = {
        "sources": {
            "executable": len(by_tier[TIER_EXECUTABLE]),
            "watch": len(by_tier[TIER_WATCH]),
            "blocked": len(by_tier[TIER_BLOCKED]),
        },
        "executable_sources": by_tier[TIER_EXECUTABLE],
        "watch_sources": by_tier[TIER_WATCH],
        "blocked_sources": by_tier[TIER_BLOCKED],
        "blocked_details": details,
        "n_sources": len(SOURCES),
        "executable": len(by_tier[TIER_EXECUTABLE]),
        "watch": len(by_tier[TIER_WATCH]),
        "blocked": len(by_tier[TIER_BLOCKED]),
        "count_unit": "sources",
        "items": None,
    }

    if items is not None:
        cnt = {TIER_EXECUTABLE: 0, TIER_WATCH: 0, TIER_BLOCKED: 0}
        for it in (items or []):
            ev = (it or {}).get("evidence") if isinstance(it, dict) else None
            tier = (ev or {}).get("tier") or TIER_WATCH   # 无徽章 → 按 watch 计，不按可执行
            cnt[tier] = cnt.get(tier, 0) + 1
        out["items"] = {"executable": cnt[TIER_EXECUTABLE], "watch": cnt[TIER_WATCH],
                        "blocked": cnt[TIER_BLOCKED]}
        out["executable"] = cnt[TIER_EXECUTABLE]
        out["watch"] = cnt[TIER_WATCH]
        out["blocked"] = cnt[TIER_BLOCKED]
        out["count_unit"] = "items"
    return out


if __name__ == "__main__":
    # 自检：python viewer/evidence_gate.py
    s = summary()
    print("来源登记 %d 条：可执行 %d / 观察 %d / 屏蔽 %d"
          % (s["n_sources"], s["sources"]["executable"],
             s["sources"]["watch"], s["sources"]["blocked"]))
    for k in SOURCES:
        e = evaluate(k)
        print("  [%-10s] %-32s %s" % (e["tier"], e["name"], e["reason"]))
    a = annotate({"symbol": "600519", "name": "贵州茅台"}, "mainline_tier")
    print("\n示例徽章：", a["evidence"])
