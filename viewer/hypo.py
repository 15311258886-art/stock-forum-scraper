# -*- coding: utf-8 -*-
"""假说闭环 —— 让大模型提假设、由系统去证伪（2026-10-31 建立）。

## 为什么需要这个文件

在此之前，大模型在复盘里说的每一句话都是「终审判决」：它说「资金在往电池链集中」，
没有人会去检验；它说「超买要回调」，而我们的实证恰恰相反（RSI6>85 时 T+5 超额
**+0.69%**），但它并不知道。**没有证伪机制，模型的偏见只会越说越自信。**

这个模块做三件事：

  1. `record_from_review`：把模型输出的 `hypotheses[]` 解析成结构化谓词，落库待测
  2. `evaluate`：用真实行情回测该谓词 —— 命中样本的 T+H 超额、按日聚合的 t 值
  3. `test_open`：批量处理所有待测假说，结论回写 `ai_hypothesis`，并沉淀进 `evo_knowledge`

结果会在**下一轮复盘**时作为证据喂回模型。这才是"判断 → 检验 → 修正"的闭环，
而不是让它每天重新猜一遍。

## 谓词为什么必须受限

不允许模型自由发挥（比如「市场情绪转暖」），只接受
`字段 运算符 数值` 形式，可用 `and` 连接两个条件。字段限白名单、运算符限六种。
理由不是不信任模型，而是**可回测性**：无法落到字段上的说法，本质上无法被证伪，
放进闭环只会制造噪音。

## 四种判定（措辞刻意保守）

  confirmed     |t|≥2 且方向一致且 |超额|≥0.30% → 可以进证据账本
  opposite      |t|≥2 且方向相反且 |超额|≥0.30% → **最有价值的一类**：直觉被数据打脸，
                                                它同时是一条反向信号
  marginal      |t|≥2 但 |超额|<0.30%        → 「统计显著、经济无意义」。这类结论
                                                **不能进规则**，否则等于把噪音包装成知识
  unsupported   |t|<2                      → 不成立，但不能说"证伪"，只能说"没测出效应"
  invalid       谓词无效/样本不足           → 不算结论，不计入模型准确率

## 口径

与全项目一致：d 日盘后知情 → **d+1 收盘建仓 → d+1+H 收盘平仓**，超额减同日全市场等权，
t 值按日聚合后再检验。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import statistics
import time
from bisect import bisect_left

log = logging.getLogger(__name__)

# 谓词可用字段 → 取数方式。新增字段必须同时在这里和 ai_review 的提示词里登记。
FIELDS = {
    "ret1":         "当日涨幅%",
    "ret2":         "两日累计涨幅%",
    "ret5":         "前5日涨幅%",
    "mom20":        "近20日涨幅%",
    "volume_ratio": "量比（当日量/含当日5日均量，上限≈5）",
    "turnover":     "龙虎榜当日换手率%（仅上榜当日有值）",
    "lhb_hit":      "近10个交易日内上过龙虎榜=1",
    "lift_days":    "距最近一次解禁还有几个交易日（无事件=-1）",
    "ann_neg":      "近20个自然日内有股东减持公告=1",
    "consec_up":    "截至当日连续上涨天数",
    "rsi6":         "RSI6",
    "macd_hist":    "MACD 柱（库字段 macdh）",
    "macd_cross_down": "MACD 柱当日由正转负=1（即「死叉」事件）",
    "bias6":        "6日乖离率",
    "ma20_dev":     "相对 MA20 的偏离%",
    "ma20_break":   "当日收盘由 ≥MA20 转为 <MA20 =1（即「破位」事件）",
    # ---- 2026-10-30 开源规则接入新增：验证 Minervini 趋势模板 / Stockbee 突破类规则 ----
    "ma60_dev":     "相对 MA60 的偏离%（中长期趋势位置）",
    "ma_align":     "均线多头排列（收盘>MA5>MA20>MA60）=1（趋势模板核心条件）",
    "dist_high250": "距近 250 日最高收盘的百分比（负值，0≈创新高，约 52 周高点）",
    "atr_pct":      "ATR/收盘×100（波动率，用于判「突破幅度是否盖过噪声」）",
}

OPS = (">=", "<=", "==", ">", "<")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_hypothesis (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id   INTEGER,
    review_date TEXT NOT NULL,
    provider    TEXT,
    statement   TEXT,
    predicate   TEXT,
    horizon     INTEGER DEFAULT 5,
    expect      TEXT,                  -- negative / positive
    rationale   TEXT,
    status      TEXT NOT NULL DEFAULT 'open',  -- open/confirmed/opposite/unsupported/invalid
    n           INTEGER,               -- 命中样本数
    days        INTEGER,               -- 覆盖的独立交易日数
    excess      REAL,
    t           REAL,
    win         REAL,
    tested_at   TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ai_hyp_stat ON ai_hypothesis(status, id);
"""

_ensured = False


def ensure_schema(conn: sqlite3.Connection) -> None:
    global _ensured
    if _ensured:
        return
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        _ensured = True
    except Exception:  # noqa: BLE001
        log.exception("[hypothesis] 建表失败")


# ---------------------------------------------------------------- 谓词解析


_COND_RE = re.compile(
    r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(>=|<=|==|>|<|=)\s*(-?\d+(?:\.\d+)?)\s*$")


def parse_predicate(text: str):
    """把 'volume_ratio >= 3 and ret1 > 5' 解析成 [(field, op, value), ...]。

    返回 None 表示不可解析（让上层标 invalid 并记录原因，而不是静默丢掉）。
    """
    if not text:
        return None
    parts = re.split(r"\s+(?:and|AND|且|并且)\s+", str(text).strip())
    if not 1 <= len(parts) <= 2:
        return None
    out = []
    for p in parts:
        m = _COND_RE.match(p.replace("＝", "=").replace("＞", ">").replace("＜", "<"))
        if not m:
            return None
        f, op, v = m.group(1).lower(), m.group(2), float(m.group(3))
        if f not in FIELDS:
            return None
        if op == "=":
            op = "=="
        out.append((f, op, v))
    return out


def _ok(val, op: str, thr: float) -> bool:
    if val is None:
        return False
    if op == ">=":
        return val >= thr
    if op == "<=":
        return val <= thr
    if op == ">":
        return val > thr
    if op == "<":
        return val < thr
    return abs(val - thr) < 1e-9


# ---------------------------------------------------------------- 外部索引


def _cal(conn) -> list:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]


def _lhb_index(conn) -> dict:
    """code -> [上榜日期...]（升序），供「近 N 日是否上榜」与换手率查询。"""
    out: dict = {}
    try:
        for code, d, tr in conn.execute(
                "SELECT security_code, trade_date, turnoverrate FROM lhb_stocks"):
            if not code or not d:
                continue
            try:
                trv = float(tr) if tr not in (None, "") else 0.0
            except Exception:  # noqa: BLE001
                trv = 0.0
            out.setdefault(code, []).append((d, trv))
    except Exception:  # noqa: BLE001
        return {}
    for c in out:
        out[c].sort()
    return out


def _ann_index(conn) -> dict:
    """code -> [减持公告日...]（升序）。只认「股东减持」类，且已剔除承诺不减持。"""
    out: dict = {}
    try:
        for code, d in conn.execute(
                "SELECT code, notice_date FROM stock_ann WHERE cat='股东减持' "
                "AND notice_date IS NOT NULL AND notice_date<>''"):
            out.setdefault(code, []).append(d)
    except Exception:  # noqa: BLE001
        return {}
    for c in out:
        out[c].sort()
    return out


def _lift_index(conn, cal: list) -> dict:
    """code -> [解禁日...]（升序）。

    只认「首发原股东」—— 实证显示定增解禁、股权激励解禁对 T+20 超额均不显著
    （见 viewer/evidence.py: lift_first_holder 的 note），放进谓词只会稀释。
    """
    out: dict = {}
    try:
        for code, fd in conn.execute(
                "SELECT code, free_date FROM lift_event WHERE type_group='首发原股东' "
                "AND free_date IS NOT NULL AND free_date<>''"):
            out.setdefault(code, []).append(str(fd)[:10])
    except Exception:  # noqa: BLE001
        return {}
    for c in out:
        out[c].sort()
    return out


# ---------------------------------------------------------------- 特征 + 筛选


def _iter_hits(conn, conds, start: str, codes=None, end: str = ""):
    """流式扫描日线，返回命中的 [(code, date)]。

    刻意不把所有日期×所有股票的特征都存进内存：5300 只 × 150 日 × 13 字段
    在 Python 里要吃掉数百 MB。这里按 code 排序流式走，只保留命中样本。

    `end` 用于时间分段稳健性检验（2026-10-30）：把样本切前后两段分别回测，
    若两段结论方向相反，说明规则不稳定，不能当规则用。
    """
    lhb = _lhb_index(conn)
    ann = _ann_index(conn)
    cal = _cal(conn)
    lift = _lift_index(conn, cal) if any(c[0] == "lift_days" for c in conds) else {}

    need_turn = any(c[0] == "turnover" for c in conds)
    need_lhb = any(c[0] == "lhb_hit" for c in conds) or need_turn
    need_lift = any(c[0] == "lift_days" for c in conds)
    need_ann = any(c[0] == "ann_neg" for c in conds)
    # 只有谓词真的用到 52 周高点时才把滚动窗口从 25 扩到 250：
    # 扩容是 O(1) 内存但每行多一次 max(250)，对现有 claim 是纯损耗。
    need_high = any(c[0] == "dist_high250" for c in conds)
    keep = 250 if need_high else 25

    sql = ("SELECT code,date,close,ma20,rsi6,macdh,bias6,volume_ratio,ma5,ma60,atr "
           "FROM stock_indicators_daily WHERE date>=? AND close>0")
    args: list = [start]
    if end:
        sql += " AND date<=?"
        args.append(str(end)[:10])
    sql += " ORDER BY code,date"

    hits: list = []
    cur_code = None
    closes: list = []           # 该 code 的历史收盘（升序，末位为当日）
    dates: list = []            # 对应日期，用于算连续上涨
    up_streak = 0
    prev_macdh = None
    prev_ma20 = None

    def flush():                # 切换 code 时重置所有滚动状态
        nonlocal closes, dates, up_streak, prev_macdh, prev_ma20
        closes, dates, up_streak = [], [], 0
        prev_macdh, prev_ma20 = None, None

    for code, d, close, ma20, rsi6, macdh, bias6, vr, ma5, ma60, atr in conn.execute(sql, args):
        if code != cur_code:
            flush()
            cur_code = code
        n = len(closes)
        ret1 = (close / closes[-1] - 1.0) * 100.0 if n >= 1 and closes[-1] else None
        ret2 = (close / closes[-2] - 1.0) * 100.0 if n >= 2 and closes[-2] else None
        ret5 = (close / closes[-6] - 1.0) * 100.0 if n >= 6 and closes[-6] else None
        mom20 = (close / closes[-21] - 1.0) * 100.0 if n >= 21 and closes[-21] else None
        up_streak = (up_streak + 1) if (ret1 is not None and ret1 > 0) else 0

        fv: dict = {
            "ret1": ret1, "ret2": ret2, "ret5": ret5, "mom20": mom20,
            "volume_ratio": vr, "rsi6": rsi6, "macd_hist": macdh,
            "bias6": bias6,
            "ma20_dev": ((close / ma20 - 1.0) * 100.0) if (ma20 and ma20 > 0) else None,
            "consec_up": float(up_streak),
            # 事件型字段：必须"当日发生"才算，不是"当前处于某状态"。
            # 这个区别很关键——「柱为负」是状态（覆盖一半的股票），「由正转负」是事件。
            "macd_cross_down": 1.0 if (macdh is not None and prev_macdh is not None
                                       and macdh < 0 <= prev_macdh) else 0.0,
            "ma20_break": 1.0 if (ma20 and prev_ma20 and close < ma20
                                  and closes[-1] >= prev_ma20) else 0.0,
        }
        # 开源规则（Minervini 趋势模板 / Stockbee 突破）需要的趋势与波动字段。
        # 全部 O(1)，除 dist_high250 外都不额外扫历史窗口。
        fv["ma60_dev"] = ((close / ma60 - 1.0) * 100.0) if (ma60 and ma60 > 0) else None
        fv["atr_pct"] = (atr / close * 100.0) if (atr and close) else None
        fv["ma_align"] = 1.0 if (ma5 and ma20 and ma60
                                 and close > ma5 > ma20 > ma60) else 0.0
        if need_high:
            hi = max(closes[-250:] + [close]) if closes else close
            fv["dist_high250"] = (close / hi - 1.0) * 100.0 if hi else None
        if need_lhb:
            arr = lhb.get(code) or []
            # 近 10 个交易日内的上榜记录（用交易日索引判断，不用自然日）
            i = bisect_left(cal, d)
            lo = cal[max(0, i - 9)] if i >= 0 else d
            recent = [(x, t) for x, t in arr if lo <= x <= d]
            fv["lhb_hit"] = 1.0 if recent else 0.0
            fv["turnover"] = max((t for _x, t in recent), default=None)
        if need_lift:
            arr = lift.get(code) or []
            fut = [x for x in arr if x >= d]
            if not fut:
                fv["lift_days"] = -1.0
            else:
                fd = fut[0]
                if fd <= cal[-1]:
                    fv["lift_days"] = float(bisect_left(cal, fd) - bisect_left(cal, d))
                else:
                    # 解禁日在未来：用「工作日 ≈ 交易日×1.4」外推，误差已在 lift.py 校准过
                    fv["lift_days"] = float(bisect_left(cal, cal[-1]) - bisect_left(cal, d)
                                            + max(1, int(_days_between(cal[-1], fd) / 1.4)))
        if need_ann:
            arr = ann.get(code) or []
            fv["ann_neg"] = 1.0 if any(_days_between(x, d) <= 20 and x <= d for x in arr[-6:]) else 0.0

        if all(_ok(fv.get(f), op, v) for f, op, v in conds):
            hits.append((code, d))

        closes.append(close)
        dates.append(d)
        prev_macdh, prev_ma20 = macdh, ma20
        if len(closes) > keep:          # 25 = 算 mom20 的窗口；250 = 算 52 周高点
            closes.pop(0)
            dates.pop(0)
    return hits


def _days_between(a: str, b: str) -> int:
    from datetime import date as _d
    try:
        return (_d.fromisoformat(str(b)[:10]) - _d.fromisoformat(str(a)[:10])).days
    except Exception:  # noqa: BLE001
        return 10 ** 6


def _tstat(vals):
    n = len(vals)
    if n < 5:
        return None
    m = statistics.fmean(vals)
    sd = statistics.stdev(vals)
    return m / (sd / n ** 0.5) if sd else None


def evaluate(conn, predicate: str, horizon: int = 5, lookback: int = 150,
             exec_off: int = 1, start: str = "", end: str = "") -> dict:
    """回测一条谓词。返回 {ok, n, days, excess, t, win, note}。

    exec_off 是**可执行性偏移**，默认 1（严格口径）：
      exec_off=1 → close[d+1] 建仓 → close[d+1+H] 平仓（信号盘后可知，次日才能买）
      exec_off=0 → close[d]   建仓 → close[d+H]   平仓（宽松口径，含当日，实际买不到）

    这个参数不是可选项而是**必要的对照**：2026-10-31 实测 RSI6>85 在宽松口径下
    +0.694%（看似"追涨有效"），严格口径却是 −0.098%（t=−2.72）。同一条件、
    同一批样本，只因为"能不能当天买到"就结论相反。做实证必须先声明口径，
    否则两个都对、两个都没用。

    start / end（2026-10-30 新增，可选）：显式限定事件日区间，用于**时间分段
    稳健性检验**。不传则退回 lookback 语义（最近 N 个交易日）。两段结论方向
    相反时，规则一律不进 active —— 一个只在半段历史上成立的规则等于没有。
    """
    conds = parse_predicate(predicate)
    if not conds:
        return {"ok": False, "note": "谓词无法解析（字段须在白名单内，格式：字段 运算符 数值）",
                "n": 0, "days": 0, "excess": None, "t": None, "win": None}
    cal = _cal(conn)
    if len(cal) < horizon + 30:
        return {"ok": False, "note": "交易日历过短", "n": 0, "days": 0,
                "excess": None, "t": None, "win": None}
    since = str(start)[:10] if start else cal[-min(len(cal) - 1, lookback)]
    hits = _iter_hits(conn, conds, since, end=end)
    if len(hits) < 12:
        return {"ok": False, "note": "命中样本不足（%d 条）" % len(hits),
                "n": len(hits), "days": 0, "excess": None, "t": None, "win": None}

    idx = {d: i for i, d in enumerate(cal)}
    off = 1 if exec_off else 0
    # 目标日期集合
    plan = []
    for code, d in hits:
        i = idx.get(d)
        if i is None or i + off + horizon >= len(cal):
            continue
        plan.append((code, cal[i + off], cal[i + off + horizon]))
    if not plan:
        return {"ok": False, "note": "无已到期的样本", "n": 0, "days": 0,
                "excess": None, "t": None, "win": None}

    # 批量取价
    codes = {c for c, _b, _e in plan}
    px: dict = {}
    CH = 400
    cl = list(codes)
    for k in range(0, len(cl), CH):
        part = cl[k:k + CH]
        ph = ",".join("?" * len(part))
        for code, d, c in conn.execute(
                "SELECT code,date,close FROM stock_indicators_daily "
                "WHERE close>0 AND code IN (%s)" % ph, part):
            px.setdefault(code, {})[d] = c

    # 各目标日的全市场等权（复用 review_score 的口径实现）
    import review_score as RS
    need = sorted({e for _c, _b, e in plan})
    stats = RS._day_stats(conn, need)

    per_day: dict = {}
    allv: list = []
    for code, b, e in plan:
        m = (stats.get(e) or {}).get("avg")
        c0 = (px.get(code) or {}).get(b)
        c1 = (px.get(code) or {}).get(e)
        if m is None or not c0 or not c1:
            continue
        ex = (c1 / c0 - 1.0) * 100.0 - m
        per_day.setdefault(e, []).append(ex)
        allv.append(ex)
    if len(allv) < 12:
        return {"ok": False, "note": "可计分样本不足", "n": len(allv), "days": 0,
                "excess": None, "t": None, "win": None}

    dv = [statistics.fmean(v) for v in per_day.values() if v]
    ex_all = statistics.fmean(allv)
    ex_day = statistics.fmean(dv)
    t_day = _tstat(dv)
    # 口径冲突检测（2026-10-31 新增，长样本才暴露）：t 值是用**按日聚合后的均值序列**
    # 算的，而原来的 excess 用的是**逐样本均值**。样本在少数几天高度聚集时，两者会分叉
    # 甚至符号相反——实测「当日跌 7%」在 2016 起的长样本里逐样本均值 +0.89%（被 2024-02
    # 那种暴跌后暴力反弹的日子拉正），按日均值却是负的、t=−11.7。此时只报一个数就是误导。
    # 约定：sign_conflict=True 时，excess 不可用，一律看 excess_day / t。
    conflict = (ex_all * ex_day < 0)
    note = ""
    if conflict:
        note = "口径冲突：逐样本均值 %+.2f%% 与按日均值 %+.2f%% 符号相反，以按日均值为准" % (
            ex_all, ex_day)
    return {
        "ok": True, "n": len(allv), "days": len(dv),
        "excess": round(ex_all, 3),
        "excess_day": round(ex_day, 3),
        "t": round(t_day, 2) if t_day is not None else None,
        "win": round(100.0 * sum(1 for v in allv if v > 0) / len(allv), 1),
        "sign_conflict": conflict,
        "note": note,
    }


# ---------------------------------------------------------------- 落库与判定


def record_from_review(conn, review_id: int, review_date: str, provider: str,
                       output: dict, elapsed_ms: int = 0) -> int:
    """把复盘输出里的 hypotheses[] 落库待测。返回条数。"""
    ensure_schema(conn)
    hyps = (output or {}).get("hypotheses") if isinstance(output, dict) else None
    if not hyps:
        return 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for h in hyps[:4]:
        if not isinstance(h, dict):
            continue
        stmt = str(h.get("statement") or "").strip()[:120]
        pred = str(h.get("predicate") or "").strip()[:160]
        if not pred:
            continue
        try:
            hz = int(h.get("horizon") or 5)
        except Exception:  # noqa: BLE001
            hz = 5
        hz = hz if hz in (1, 3, 5, 10, 20) else 5
        exp = str(h.get("expect") or "").lower()
        if exp not in ("negative", "positive"):
            exp = "negative" if exp in ("负", "-", "down") else "positive"
        try:
            conn.execute(
                "INSERT INTO ai_hypothesis(review_id,review_date,provider,statement,predicate,"
                "horizon,expect,rationale,status,created_at) VALUES(?,?,?,?,?,?,?,?,'open',?)",
                (int(review_id), review_date, provider, stmt, pred, hz, exp,
                 str(h.get("rationale") or "")[:300], now))
            n += 1
        except Exception:  # noqa: BLE001
            log.exception("[hypothesis] 写入失败 pred=%s", pred)
    if n:
        conn.commit()
    return n


# 最小有效幅度（%）。低于它一律不进「成立」—— 见文件头「显著 ≠ 可用」。
# 0.30% 的来历：A 股双边手续费约 0.02%~0.05%，加滑点与冲击成本，实际往返约 0.15%~0.25%。
# 取 0.30% 作为「扣完成本还剩一点」的下限。这是经验值而非精确阈值，
# 但它能把「t=−2.7 却只有 −0.10%」这类统计显著、经济无意义的结论挡在外面。
MIN_EFFECT = 0.30


def test_open(conn, limit: int = 20) -> dict:
    """回测所有 open 的假说，写结论，并把**真正可用**的沉淀进 evo_knowledge。"""
    ensure_schema(conn)
    rows = list(conn.execute(
        "SELECT * FROM ai_hypothesis WHERE status='open' ORDER BY id LIMIT ?", (limit,)))
    res = {"tested": 0, "confirmed": 0, "opposite": 0, "marginal": 0,
           "unsupported": 0, "invalid": 0}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        try:
            e = evaluate(conn, r["predicate"], int(r["horizon"] or 5))
        except Exception as ex:  # noqa: BLE001
            log.exception("[hypothesis] 回测异常 id=%s", r["id"])
            e = {"ok": False, "note": "回测异常：%s" % str(ex)[:150], "n": 0, "days": 0,
                 "excess": None, "t": None, "win": None}
        if not e.get("ok"):
            status = "invalid"
            res["invalid"] += 1
        else:
            t = e.get("t")
            if t is None or abs(t) < 2.0:
                status = "unsupported"
                res["unsupported"] += 1
            elif abs(e.get("excess") or 0) < MIN_EFFECT:
                # 方向可能对、统计也显著，但幅度扣掉成本后什么都不剩。
                # 这种结论绝不能进规则，否则等于把噪音包装成知识 ——
                # 实测踩过：RSI6>85 在 T+5 是 −0.10%/t=−2.72，第一版判定直接给了
                # confirmed 并沉淀进 evo_knowledge，等于教模型一个没用的"经验"。
                status = "marginal"
                res["marginal"] += 1
            else:
                got_neg = e["excess"] < 0
                want_neg = (r["expect"] == "negative")
                status = "confirmed" if got_neg == want_neg else "opposite"
                res[status] += 1
        try:
            conn.execute(
                "UPDATE ai_hypothesis SET status=?,n=?,days=?,excess=?,t=?,win=?,tested_at=?,"
                "note=? WHERE id=?",
                (status, e.get("n"), e.get("days"), e.get("excess"), e.get("t"),
                 e.get("win"), now, (e.get("note") or "")[:300], r["id"]))
        except Exception:  # noqa: BLE001
            log.exception("[hypothesis] 回填失败 id=%s", r["id"])
            continue
        res["tested"] += 1
        if status in ("confirmed", "opposite"):
            _write_evo(conn, r, e, status)
    conn.commit()
    return res


def _write_evo(conn, row, e: dict, status: str) -> None:
    """把结论沉淀进 evo_knowledge（ai_review 的 doctrine 段会读它）。"""
    try:
        kid = "K-HYP-%d" % int(row["id"])
        if status == "confirmed":
            title = "数据自生长规则（模型提出·已回测）：%s" % (row["statement"] or row["predicate"])
            body = ("当 %s 时，T+%d 超额 %+.2f%%（t=%+.2f，胜率 %.1f%%，n=%d/%d 个交易日）。"
                    "提出依据：%s"
                    % (row["predicate"], row["horizon"], e["excess"], e["t"], e["win"],
                       e["n"], e["days"], row["rationale"] or "-"))
            kind = "结论"
        else:
            title = "被回测推翻的说法（模型提出·方向相反）：%s" % (row["statement"] or row["predicate"])
            body = ("模型预期 %s，实测 T+%d 超额 %+.2f%%（t=%+.2f，n=%d），**方向相反**。"
                    "该说法不得再作为判断依据；反向是否可作信号需单独验证。"
                    % (row["expect"], row["horizon"], e["excess"], e["t"], e["n"]))
            kind = "反例"
        conn.execute(
            "INSERT OR REPLACE INTO evo_knowledge(kid,kind,title,body,evidence,sources,status) "
            "VALUES(?,?,?,?,?,?,?)",
            (kid, kind, title[:180], body[:600],
             "predicate=%s; n=%s; days=%s" % (row["predicate"], e["n"], e["days"]),
             "ai_hypothesis#%s/%s" % (row["id"], row["review_date"]), "已验证"))
    except Exception:  # noqa: BLE001
        log.exception("[hypothesis] 沉淀失败 id=%s", row["id"])


# ---------------------------------------------------------------- 给模型读


def prompt_block(conn, limit: int = 6) -> str:
    """把已检验的假说结果整理成给模型读的文本。空则返回空串。"""
    try:
        rows = list(conn.execute(
            "SELECT statement,predicate,horizon,expect,status,excess,t,n,days,note "
            "FROM ai_hypothesis WHERE status<>'open' ORDER BY id DESC LIMIT ?", (limit,)))
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return ""
    L = ["【你之前提的假说，系统已经回测过了】"]
    lab = {"confirmed": "成立", "opposite": "**方向相反**",
           "marginal": "显著但幅度不足（扣成本后无意义，不可用）",
           "unsupported": "未测出效应", "invalid": "无效（无法回测）"}
    for r in rows:
        if r[4] in ("invalid",):
            L.append("· 「%s」→ %s：%s" % (r[0] or r[1], lab[r[4]], r[9] or ""))
            continue
        L.append("· 「%s」当 %s → T+%d 超额 %+.2f%%（t=%+.2f，n=%s/%s日）→ %s"
                 % (r[0] or "-", r[1], r[2],
                    r[5] if r[5] is not None else 0,
                    r[6] if r[6] is not None else 0,
                    r[7] or 0, r[8] or 0, lab.get(r[4], r[4])))
    L.append("提新假说前先看这一段：已成立的可复用，方向相反的不要再提。")
    return "\n".join(L)


def report(conn, limit: int = 30) -> dict:
    ensure_schema(conn)
    rows = list(conn.execute(
        "SELECT id,review_date,statement,predicate,horizon,expect,status,n,days,excess,t,win,note "
        "FROM ai_hypothesis ORDER BY id DESC LIMIT ?", (limit,)))
    keys = ("id", "review_date", "statement", "predicate", "horizon", "expect", "status",
            "n", "days", "excess", "t", "win", "note")
    out = {"items": [dict(zip(keys, r)) for r in rows], "counts": {}}
    for st, c in conn.execute("SELECT status, COUNT(*) FROM ai_hypothesis GROUP BY status"):
        out["counts"][st] = c
    return out


if __name__ == "__main__":
    # 自检：python viewer/hypo.py
    import os
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    print("=== 谓词解析自检 ===")
    for s in ("volume_ratio >= 3 and ret1 > 5", "lhb_hit == 1", "ret5>=10",
              "情绪转暖", "turnover >= 30 and ann_neg == 1", "rsi6 > 85"):
        print("  %-42s -> %s" % (s, parse_predicate(s)))
    print("\n=== 实时回测演示（用已有实证做对照）===")
    for s, h, exp in (("rsi6 > 85", 5, "negative"), ("lhb_hit == 1 and turnover >= 20", 5, "negative"),
                      ("ma20_dev < -10", 5, "negative")):
        e = evaluate(con, s, h)
        print("  %-38s -> %s" % (s, e))
