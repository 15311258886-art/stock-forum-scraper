# -*- coding: utf-8 -*-
"""开源情报挖掘 —— 「别人写在 GitHub 上的规则，在我们的数据上到底成不成立」（2026-10-30 建立）。

## 为什么单独立一个文件

进哥的判断是对的：GitHub 上的量化项目密集、免费、且**规则是明牌的**——比论坛里
「大 V 说这票要涨」有价值得多，因为规则可以被证伪。

但直接抄会踩三个坑：

  ① 高星 ≠ 高价值。星数最高的是回测框架（akquant 2.4k★）和黑盒模型
     （LSTM/Transformer 一类），它们不产生可执行规则，只产生不可解释的曲线。
  ② 他们的回测大多不能用。样本小（几千行）、单票时间序列、常带未来函数。
     同一个「4% 突破」在美股 ETF 上有效，在 A 股 T+1 严格口径下可能正好相反。
  ③ 规则一旦照抄进代码，就没人知道它到底有没有效——最后变成又一批「说法」。

所以这里的定位很窄：**只做「抽规则 → 用我们自己的口径重测 → 记账」这一件事。**
不抄代码（License 与数据生态都不匹配），只抽「条件 + 方向 + 为什么可能有效」。

## 口径

与全项目完全一致，直接调用 `viewer/hypo.evaluate()`：
  d 盘后知情 → d+1 收盘建仓 → d+1+H 收盘平仓，超额减同日全市场等权，t 按日聚合。

并额外加一道**分段稳健性**：把样本日切前后两段分别回测。
只在半段历史上成立的规则，一律不给 active —— 那是曲线拟合，不是知识。

## 状态

  active       方向符合预期、|t|≥2、|超额|≥0.3%、且两段同向 → 可进规则
  refuted      方向与预期相反且幅度够 → **禁止使用**，并写进 AI 复盘上下文
  marginal     |t|≥2 但 |超额|<0.3% → 显著但无利可图
  unsupported  |t|<2 → 没测出效应
  unstable     均值和 t 符号相反，或两段方向相反 → 结论不可靠
  invalid      谓词无效 / 样本不足

`unstable` 这一条是本次自检时补的：`dist_high250 >= -5` 会得到「均值 +0.048%、
t=−3.3」这种自相矛盾的组合——少数极端上涨把总体均值拉正，而按日平均后多数日子
是负的。这种数不经检查就会被当成「贴近新高会涨」写进结论，必须显式拦掉。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import hypo as HP

log = logging.getLogger(__name__)

ASOF = "2026-10-30"

# 规则结果缓存天数：到期重测（防止一次测完就永远相信）
STALE_DAYS = 30

# ---------------------------------------------------------------- 规则种子
#
# 每条规则都必须能回答三个问题：出自哪、原版怎么说、我们改成什么谓词。
# 抽规则不抽代码：谓词只能表达「字段 运算符 数值」，表达不了的（多因子打分、
# 排序分位）就不进这里，硬塞进来只会测出一个和原规则无关的东西。

RULE_SEEDS: tuple = (
    # ============ Mark Minervini 趋势模板（RyanJHamby/stock-screener, 71★）============
    # 原版 8 条硬标准：价>MA150&MA200、MA150>MA200、MA200 上行、价>MA50、
    # 距 52 周低点 +30%、距 52 周高点 25% 内、RS 强度≥70。
    # 我们能落地的只有均线排列与距高点两条，RS 强度需要全市场分位（谓词表达不了）。
    {
        "key": "minervini_trend", "repo": "RyanJHamby/stock-screener",
        "title": "趋势模板·均线多头排列",
        "rule": "收盘 > MA5 > MA20 > MA60（趋势模板的均线核心条件）",
        "predicate": "ma_align == 1", "horizon": 5, "expect": "positive",
        "why": "开源趋势跟踪的基石条件，几乎所有 A 股选股 skill 都在用同一套均线多头逻辑。",
    },
    {
        "key": "minervini_near_high", "repo": "RyanJHamby/stock-screener",
        "title": "趋势模板·距 52 周高 25% 内",
        "rule": "距近 250 日最高收盘不超过 25%",
        "predicate": "dist_high250 >= -25", "horizon": 5, "expect": "positive",
        "why": "原版要求「距高点 25% 内」，隐含「强势股继续强」的动量假设。",
    },
    {
        "key": "minervini_breakout_tight", "repo": "RyanJHamby/stock-screener",
        "title": "趋势模板·贴着新高（5% 内）",
        "rule": "距近 250 日最高收盘不超过 5%",
        "predicate": "dist_high250 >= -5", "horizon": 5, "expect": "positive",
        "why": "最严格的突破形态，原版用它筛「随时起爆」的票。",
    },
    # ============ Stockbee 动量爆发（dcimring/stockbee-dashboard, 5★）============
    # 原版：4% 涨幅 + 成交量放大 = momentum burst，之后 5 日看延续。
    {
        "key": "stockbee_burst", "repo": "dcimring/stockbee-dashboard",
        "title": "Stockbee·4% 放量突破",
        "rule": "当日涨幅 ≥4% 且量比 ≥1.5",
        "predicate": "ret1 >= 4 and volume_ratio >= 1.5", "horizon": 5, "expect": "positive",
        "why": "原版 momentum burst 的核心触发条件，被大量 A 股短线策略照搬。",
    },
    {
        "key": "stockbee_burst_novol", "repo": "dcimring/stockbee-dashboard",
        "title": "Stockbee·纯 4% 突破（剥离放量）",
        "rule": "当日涨幅 ≥4%（不看量）",
        "predicate": "ret1 >= 4", "horizon": 5, "expect": "positive",
        "why": "分组对照：用来分离「涨幅」和「放量」各自的贡献。",
    },
    # ============ 回调买点（多个 A 股 skill 共有）============
    {
        "key": "pullback_ma20", "repo": "online0001/short-term-stock-picker",
        "title": "回踩不破 MA20",
        "rule": "收盘在 MA20 上方但偏离不超过 0（即回踩到均线附近不破）",
        "predicate": "ma20_dev >= -3", "horizon": 5, "expect": "positive",
        "why": "「回踩均线是买点」是中文量化社区最普遍的说法之一，需验证。",
    },
    {
        "key": "shrink_volume", "repo": "online0001/short-term-stock-picker",
        "title": "缩量（量价健康度）",
        "rule": "当日量比 ≤0.7",
        "predicate": "volume_ratio <= 0.7", "horizon": 5, "expect": "positive",
        "why": "「缩量回调是洗盘、可买」的量化表达。",
    },
    # ============ 动量 / 反转（Factor-Research, 422★）============
    {
        "key": "momentum_20", "repo": "nuglifeleoji/Factor-Research",
        "title": "20 日动量 >20%",
        "rule": "近 20 个交易日涨幅 ≥20%",
        "predicate": "mom20 >= 20", "horizon": 5, "expect": "positive",
        "why": "动量因子是海外因子库里最稳的一类（12-1 动量），需看 A 股短周期是否同构。",
    },
    {
        "key": "reversal_20", "repo": "nuglifeleoji/Factor-Research",
        "title": "20 日跌幅 >20%（反转）",
        "rule": "近 20 个交易日跌幅 ≥20%",
        "predicate": "mom20 <= -20", "horizon": 5, "expect": "positive",
        "why": "A 股散户结构下短期反转常被提起，与动量方向相反，两条一起测才有对照意义。",
    },
    # ============ 波动率（通用风控）============
    {
        "key": "high_vol", "repo": "nuglifeleoji/Factor-Research",
        "title": "高波动（ATR/价 >6%）",
        "rule": "ATR 占收盘价比例 ≥6%",
        "predicate": "atr_pct >= 6", "horizon": 5, "expect": "negative",
        "why": "低波异象在海外很稳（低波跑赢高波），验证 A 股是否同向。",
    },
    # ============ 已被我们自己推翻、但开源社区仍在传的 ============
    {
        "key": "rsi_overbought", "repo": "多个 A 股 skill（通用说法）",
        "title": "RSI 超买（>80）",
        "rule": "RSI6 ≥80",
        "predicate": "rsi6 >= 80", "horizon": 5, "expect": "negative",
        "why": "「超买必回调」是最流行的说法，2026-10-31 已在宽松口径下被证伪，这里做二次确认。",
    },
)


# ---------------------------------------------------------------- 仓库候选池
#
# 2026-10-30 首轮扫描结果（GitHub 检索：A股量化 / 涨停打板 / alpha factor / market regime）。
# bucket：A=规则可翻译可直接回测；B=结构思路值得读；C=黑盒或纯数据源，不采用。
# 这张表是「发现」的留痕，避免同一个仓库被反复挖三遍。

SEED_REPOS: tuple = (
    {"full_name": "akfamily/akquant", "stars": 2477, "lang": "Python",
     "descr": "Rust+Python 高性能量化回测框架（akshare 同门）",
     "bucket": "B", "verdict": "skip",
     "why": "工程实现值得看，但没有可直接抽的 alpha 规则；我们已有自己的严格口径引擎。"},
    {"full_name": "nuglifeleoji/Factor-Research", "stars": 422, "lang": "Jupyter",
     "descr": "158 个 alpha 因子 + LSTM/ResNet，自称 IC=0.065",
     "bucket": "A", "verdict": "mining",
     "why": "因子清单可抽（动量/波动率已入库）；模型部分不可信，不采用。"},
    {"full_name": "shouldnotappearcalm/a-share-skill", "stars": 255, "lang": "Python",
     "descr": "A股数据/选股/模拟交易 Skill 集合（Claude Code 形态）",
     "bucket": "B", "verdict": "read",
     "why": "skill 的拆法（工具粒度、上下文注入方式）对我们 AI 复盘有借鉴价值。"},
    {"full_name": "pseudo-longinus/quant-buddy-skills", "stars": 197, "lang": "Python",
     "descr": "面向 Agent 的投资研究 skill 底座（A股/港股/美股/期货/ETF/宏观）",
     "bucket": "B", "verdict": "read",
     "why": "同上，看它的能力边界划分；其数据层我们已自建。"},
    {"full_name": "RyanJHamby/stock-screener", "stars": 71, "lang": "Python",
     "descr": "Minervini 趋势模板 8 条硬标准 + 市场 regime 过滤",
     "bucket": "A", "verdict": "mining",
     "why": "规则最明确，三条已翻译成谓词回测（见 oss_rule.minervini_*）。"},
    {"full_name": "online0001/short-term-stock-picker", "stars": 58, "lang": "Python",
     "descr": "A股打板/涨停/均线/量价健康度 skill 集合",
     "bucket": "A", "verdict": "mining",
     "why": "「回踩 MA20」「缩量洗盘」两条说法已入库回测。"},
    {"full_name": "dcimring/stockbee-dashboard", "stars": 5, "lang": "Python",
     "descr": "Stockbee 波段法：市场宽度 + 4% 动量爆发 + episodic pivot",
     "bucket": "A", "verdict": "mining",
     "why": "典型「低星高价值」：规则全是明牌，4% 突破已回测；宽度指标待做（需市场层口径）。"},
    {"full_name": "SixCake/ticai-daban", "stars": 8, "lang": "Python",
     "descr": "A股题材打板：涨停事件库 + 独立归属 + 题材天梯",
     "bucket": "A", "verdict": "watch",
     "why": "与我们的 harvest 引擎高度重合，看它的「题材独立归属」算法是否有增量。"},
)


# ---------------------------------------------------------------- 建表

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oss_rule (
    key         TEXT PRIMARY KEY,
    repo        TEXT,
    title       TEXT,
    rule        TEXT,
    predicate   TEXT,
    horizon     INTEGER DEFAULT 5,
    expect      TEXT,
    why         TEXT,
    n           INTEGER,
    days        INTEGER,
    excess      REAL,
    t           REAL,
    win         REAL,
    status      TEXT,
    note        TEXT,
    seg1_label  TEXT, seg1_n INTEGER, seg1_excess REAL, seg1_t REAL,
    seg2_label  TEXT, seg2_n INTEGER, seg2_excess REAL, seg2_t REAL,
    stable      INTEGER,
    tested_at   TEXT,
    elapsed     REAL,
    cross_label TEXT,
    cross_n     INTEGER,
    cross_days  INTEGER,
    cross_excess REAL,
    cross_t     REAL,
    cross_win   REAL,
    cross_same  INTEGER
);
CREATE TABLE IF NOT EXISTS oss_repo (
    full_name   TEXT PRIMARY KEY,
    stars       INTEGER,
    lang        TEXT,
    descr       TEXT,
    bucket      TEXT,
    verdict     TEXT,
    why         TEXT,
    first_seen  TEXT,
    last_seen   TEXT
);
CREATE INDEX IF NOT EXISTS ix_oss_rule_status ON oss_rule(status);
"""

_ensured = False


# ---------------------------------------------------------------- 跨周期复核
#
# 为什么必须有这一段：2026-10-30 自检时 `stock_indicators_daily` 只从 2025-08-18 开始
# （288 个交易日，约 14 个月），hypo 的所有回测都建在这张表上，**主结论的样本深度只有
# 14 个月**、未覆盖完整牛熊 —— 这是当时最大的口径缺陷。
# 2026-10-31 因子表已回补到 2016-01-04（2627 个交易日），主结论样本不再是 14 个月；
# 但库里的 oss_rule 历史行仍是 288 天算出来的，跨周期复核这一段继续保留，
# 用来给「旧行 vs 长样本」做对照，直到整表重跑完毕。
#
# `data/kline_daily.db` 有 1990 至今的 1,700 万行原始日线。这里用窗口函数在库内
# 直接算 MA/量比/前向收益，把核心规则放到 2020 年以来的长样本上再验一遍：
# 两套样本结论同向 → 可信度显著提升；反向 → 说明规则只在近一年成立，不能当规则。

KLINE_DB = os.path.join("data", "kline_daily.db")

# 只复核结论最强的两条（均线多头、4% 放量突破），全量复核太慢且边际收益低。
_CROSS_SQL = """
WITH t AS (
  SELECT code, d, c, v,
    AVG(c) OVER (PARTITION BY code ORDER BY d ROWS BETWEEN 4  PRECEDING AND CURRENT ROW) ma5,
    AVG(c) OVER (PARTITION BY code ORDER BY d ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) ma20,
    AVG(c) OVER (PARTITION BY code ORDER BY d ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) ma60,
    AVG(v) OVER (PARTITION BY code ORDER BY d ROWS BETWEEN 4  PRECEDING AND CURRENT ROW) vma5,
    LAG(c)     OVER (PARTITION BY code ORDER BY d) pc,
    LEAD(c, 1) OVER (PARTITION BY code ORDER BY d) e1,
    LEAD(c, 6) OVER (PARTITION BY code ORDER BY d) e6
  FROM kline WHERE d >= :since
), f AS (
  SELECT d,
    (e6 / e1 - 1.0) * 100.0 AS fwd,
    CASE WHEN c > ma5 AND ma5 > ma20 AND ma20 > ma60 THEN 1 ELSE 0 END AS a_align,
    CASE WHEN (c / pc - 1.0) * 100.0 >= 4 AND vma5 > 0 AND v / vma5 >= 1.5
         THEN 1 ELSE 0 END AS a_burst
  FROM t
  WHERE e1 > 0 AND e6 > 0 AND c > 0 AND pc > 0 AND ma60 IS NOT NULL
)
SELECT d, COUNT(*) AS n, AVG(fwd) AS mkt,
       SUM(CASE WHEN a_align = 1 THEN fwd ELSE 0 END) AS sa, SUM(a_align) AS na,
       SUM(CASE WHEN a_burst = 1 THEN fwd ELSE 0 END) AS sb, SUM(a_burst) AS nb
FROM f GROUP BY d ORDER BY d
"""


def _kline_path() -> str:
    for p in (KLINE_DB, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "data", "kline_daily.db")):
        if os.path.exists(p):
            return p
    return ""


def _tstat(vals):
    import statistics
    n = len(vals)
    if n < 5:
        return None
    sd = statistics.stdev(vals)
    if sd == 0:
        return None
    return statistics.fmean(vals) / (sd / (n ** 0.5))


def crosscheck_kline(since: int = 20200101) -> dict:
    """用 2020 年至今的全量日线复核两条核心规则。返回 {status, span, rules{}}。

    基准：同日全市场等权前向收益（与 hypo 的 demean 口径一致，但为库内一次性
    近似复算，不是逐笔同源——数字会有小数点级差异，方向是可靠的）。
    """
    p = _kline_path()
    if not p:
        return {"ok": False, "reason": "日线库缺失"}
    t0 = time.time()
    con = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
    try:
        rows = con.execute(_CROSS_SQL, {"since": since}).fetchall()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "复核查询失败：%s" % str(e)[:120]}
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    if not rows:
        return {"ok": False, "reason": "无可用日线"}

    def summarize(idx_sum: int, idx_cnt: int):
        daily = []
        for row in rows:
            mkt, s, k = row[2], row[idx_sum], row[idx_cnt]
            if not k or mkt is None or s is None:
                continue
            daily.append(s / k - mkt)      # 当日命中票平均收益 − 当日全市场等权
        if len(daily) < 30:
            return None
        ts = _tstat(daily)
        return {"days": len(daily),
                "excess": round(sum(daily) / len(daily), 3),
                "t": round(ts, 2) if ts is not None else None}

    out = {}
    a = summarize(3, 4)      # sa, na
    b = summarize(5, 6)      # sb, nb
    if a:
        out["minervini_trend"] = a
    if b:
        out["stockbee_burst"] = b
    return {"ok": bool(out), "span": "%d 年以来（长样本）" % (since // 10000),
            "rules": out, "elapsed": round(time.time() - t0, 1)}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """按连接实际状态建表 + 补列。

    2026-10-30 自检修正：原实现用模块级布尔 `_ensured` 做「只建一次」缓存。
    但同一个进程里完全可能换连接（DB 重连、自检用内存库、集成测试），
    第一个连接建过表后布尔就置 True，第二个连接（空库）便不再建表，
    接下来的查询直接 `no such table` 抛异常。改为每次查一次 sqlite_master
    —— 微秒级开销，换掉一个只在换库时才爆、极难复现的坑。
    """
    try:
        hit = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oss_rule'").fetchone()
        if not hit:
            conn.executescript(_SCHEMA)
            conn.commit()
        _migrate(conn)
    except Exception:  # noqa: BLE001
        log.exception("[oss] 建表失败")


_CROSS_COLS = (("cross_label", "TEXT"), ("cross_n", "INTEGER"), ("cross_days", "INTEGER"),
               ("cross_excess", "REAL"), ("cross_t", "REAL"), ("cross_win", "REAL"),
               ("cross_same", "INTEGER"))


def _migrate(conn) -> None:
    """老库补列（2026-10-30 加跨周期复核字段）。表已存在时 CREATE 不会加列。"""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(oss_rule)")}
        if not cols:
            return
        added = False
        for c, t in _CROSS_COLS:
            if c not in cols:
                conn.execute("ALTER TABLE oss_rule ADD COLUMN %s %s" % (c, t))
                added = True
        if added:
            conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("[oss] 补列失败")


# ---------------------------------------------------------------- 判定

def _verdict(expect: str, excess: float, t: float, n: int, days: int,
             stable: int) -> str:
    """把回测数字判成一个状态。阈值与 evidence.py 完全一致。"""
    if not n or n < 30 or not days or days < 10 or t is None or excess is None:
        return "invalid"
    # 均值与 t 符号相反 = 分布极度偏斜（少数极端值撑起均值），结论不可用
    if excess != 0 and (excess > 0) != (t > 0):
        return "unstable"
    if abs(t) < 2:
        return "unsupported"
    if abs(excess) < 0.30:
        return "marginal"
    good = (excess > 0) if expect == "positive" else (excess < 0)
    if not good:
        return "refuted"
    if not stable:
        return "unstable"
    return "active"


def _seg_label(lo: str, hi: str) -> str:
    return "%s~%s" % (str(lo)[:7], str(hi)[:7])


# ---------------------------------------------------------------- 回测

def test_one(conn, rule: dict, lookback: int = 400, robust: bool = True) -> dict:
    """跑一条规则：主区间 + 前后两段稳健性。返回落库字段。"""
    t0 = time.time()
    pred = rule["predicate"]
    h = int(rule.get("horizon") or 5)
    main = HP.evaluate(conn, pred, h, lookback=lookback)

    seg = {}
    stable = 1
    if robust and main.get("ok"):
        cal = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]
        if len(cal) >= 60:
            lo = cal[-min(len(cal), lookback)]
            mid = cal[len(cal) // 2]
            hi = cal[-1]
            for tag, a, b in (("seg1", lo, mid), ("seg2", mid, hi)):
                r = HP.evaluate(conn, pred, h, start=a, end=b)
                seg[tag] = {"label": _seg_label(a, b), "n": r.get("n"),
                            "excess": r.get("excess"), "t": r.get("t"),
                            "ok": bool(r.get("ok"))}
            # 两段都有结论且方向相反 → 不稳
            s1, s2 = seg.get("seg1"), seg.get("seg2")
            if (s1 and s2 and s1["ok"] and s2["ok"]
                    and s1["excess"] is not None and s2["excess"] is not None):
                if (s1["excess"] > 0) != (s2["excess"] > 0):
                    stable = 0

    status = _verdict(rule.get("expect") or "positive", main.get("excess"),
                      main.get("t"), main.get("n") or 0, main.get("days") or 0, stable)

    note = ""
    if not main.get("ok"):
        note = main.get("note") or "回测失败"
    elif status == "unstable" and stable == 0:
        note = "前后两段方向相反，规则不稳定"
    elif status == "unstable":
        note = "均值与 t 符号相反（分布极度偏斜），数字不可当结论用"
    elif status == "refuted":
        note = "实测方向与开源说法相反"
    elif status == "marginal":
        note = "统计显著但幅度不够覆盖成本"

    row = {
        "key": rule["key"], "repo": rule.get("repo", ""), "title": rule.get("title", ""),
        "rule": rule.get("rule", ""), "predicate": pred, "horizon": h,
        "expect": rule.get("expect", "positive"), "why": rule.get("why", ""),
        "n": main.get("n") or 0, "days": main.get("days") or 0,
        "excess": main.get("excess"), "t": main.get("t"), "win": main.get("win"),
        "status": status, "note": note,
        "seg1_label": (seg.get("seg1") or {}).get("label"),
        "seg1_n": (seg.get("seg1") or {}).get("n"),
        "seg1_excess": (seg.get("seg1") or {}).get("excess"),
        "seg1_t": (seg.get("seg1") or {}).get("t"),
        "seg2_label": (seg.get("seg2") or {}).get("label"),
        "seg2_n": (seg.get("seg2") or {}).get("n"),
        "seg2_excess": (seg.get("seg2") or {}).get("excess"),
        "seg2_t": (seg.get("seg2") or {}).get("t"),
        "stable": stable,
        "tested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed": round(time.time() - t0, 1),
    }
    return row


def _upsert_rule(conn, row: dict) -> None:
    cols = ("key,repo,title,rule,predicate,horizon,expect,why,n,days,excess,t,win,"
            "status,note,seg1_label,seg1_n,seg1_excess,seg1_t,seg2_label,seg2_n,"
            "seg2_excess,seg2_t,stable,tested_at,elapsed").split(",")
    ph = ",".join("?" * len(cols))
    conn.execute("INSERT OR REPLACE INTO oss_rule (%s) VALUES (%s)" % (",".join(cols), ph),
                 [row.get(c) for c in cols])


def _seed_repos(conn) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = 0
    for r in SEED_REPOS:
        cur = conn.execute("SELECT 1 FROM oss_repo WHERE full_name=?", (r["full_name"],))
        if cur.fetchone():
            conn.execute("UPDATE oss_repo SET stars=?, last_seen=? WHERE full_name=?",
                         (r["stars"], now, r["full_name"]))
        else:
            conn.execute("INSERT INTO oss_repo (full_name,stars,lang,descr,bucket,verdict,"
                         "why,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                         (r["full_name"], r["stars"], r["lang"], r["descr"],
                          r["bucket"], r["verdict"], r["why"], now, now))
            n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------- GitHub 扫描（可选）
#
# 服务端每天扫一次。失败一律静默降级 —— 网络抖动不能连累本地回测，
# 更不能让「今天没扫到」被误读成「没有新东西」。

QUERIES = (
    "A股 量化 选股",
    "涨停 打板",
    "alpha factor stock prediction",
    "stock market regime",
)


def scan_github(days: int = 14, per_query: int = 8) -> list:
    """按关键词扫 GitHub 近 N 天有更新的仓库。返回候选列表（可能为空）。"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out: list = []
    seen = set()
    for q in QUERIES:
        query = "%s pushed:>%s" % (q, since)
        url = ("https://api.github.com/search/repositories?q=%s&sort=stars&order=desc&per_page=%d"
               % (urllib.parse.quote(query), per_query))
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "oss-mine/1.0", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            log.info("[oss] 扫描失败（%s）：%s", q, e)
            continue
        for it in (data.get("items") or []):
            fn = it.get("full_name")
            if not fn or fn in seen:
                continue
            seen.add(fn)
            out.append({"full_name": fn, "stars": it.get("stargazers_count") or 0,
                        "lang": it.get("language") or "", "descr": (it.get("description") or "")[:180]})
        time.sleep(0.8)      # 未认证 API 限 10 次/分钟，宁可慢
    return out


def _merge_scanned(conn, items: list) -> int:
    """新扫到的仓库入池，标 bucket='?' 待人工/模型分级（不自动判，避免噪声入库）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new = 0
    for it in items:
        if conn.execute("SELECT 1 FROM oss_repo WHERE full_name=?", (it["full_name"],)).fetchone():
            continue
        conn.execute("INSERT INTO oss_repo (full_name,stars,lang,descr,bucket,verdict,why,"
                     "first_seen,last_seen) VALUES (?,?,?,?,'?','new','',?,?)",
                     (it["full_name"], it["stars"], it["lang"], it["descr"], now, now))
        new += 1
    conn.commit()
    return new


def _write_cross(conn, key: str, c: dict, main_excess, span: str) -> None:
    """把长样本复核结果写进对应规则行，并标两套样本方向是否一致。"""
    same = 1 if (main_excess is not None and c.get("excess") is not None
                 and (main_excess > 0) == (c["excess"] > 0)) else 0
    conn.execute("UPDATE oss_rule SET cross_label=?,cross_n=?,cross_days=?,cross_excess=?,"
                 "cross_t=?,cross_same=? WHERE key=?",
                 (span, c.get("n"), c.get("days"), c.get("excess"), c.get("t"), same, key))
    conn.commit()


# ---------------------------------------------------------------- 刷新入口

def refresh(conn, lookback: int = 400, force: bool = False, scan: bool = True,
            keys=None, cross: bool = True) -> dict:
    """完整跑一轮：扫描 → 种子入库 → 逐条回测 → 落表。

    force=False 时跳过 STALE_DAYS 内已测过的规则（日常调度走这条路，几乎零开销）。
    """
    ensure_schema(conn)
    t0 = time.time()
    scanned = 0
    if scan:
        try:
            scanned = _merge_scanned(conn, scan_github())
        except Exception:  # noqa: BLE001
            log.exception("[oss] 扫描异常（已忽略）")
    _seed_repos(conn)

    rd = {r[0]: r[1] for r in conn.execute("SELECT key, tested_at FROM oss_rule")}
    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    done, skipped, failed = 0, 0, 0
    for rule in RULE_SEEDS:
        if keys and rule["key"] not in keys:
            continue
        if not force and rd.get(rule["key"], "") > cutoff:
            skipped += 1
            continue
        try:
            row = test_one(conn, rule, lookback=lookback, robust=True)
            _upsert_rule(conn, row)
            conn.commit()
            done += 1
        except Exception as e:  # noqa: BLE001 —— 一条规则炸不能连累整轮
            log.exception("[oss] 规则 %s 回测失败", rule["key"])
            failed += 1
            try:
                conn.execute("UPDATE oss_rule SET note=? WHERE key=?",
                             ("回测异常：%s" % str(e)[:120], rule["key"]))
                conn.commit()
            except Exception:  # noqa: BLE001
                pass
    # ---- 长样本复核（2020 年至今全量日线）----
    cross_info: dict = {}
    if cross:
        try:
            cross_info = crosscheck_kline()
            if cross_info.get("ok"):
                ex = {r["key"]: r.get("excess")
                      for r in _rows(conn, "SELECT key,excess FROM oss_rule")}
                for k, c in (cross_info.get("rules") or {}).items():
                    _write_cross(conn, k, c, ex.get(k), cross_info.get("span") or "")
        except Exception:  # noqa: BLE001 —— 复核失败不能抹掉主结果
            log.exception("[oss] 跨周期复核异常（已忽略，主结果保留）")

    return {"ok": True, "scanned": scanned, "tested": done, "skipped": skipped,
            "failed": failed, "cross": bool((cross_info or {}).get("ok")),
            "cross_elapsed": (cross_info or {}).get("elapsed"),
            "elapsed": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- 读接口

_STATUS_ORDER = {"active": 0, "refuted": 1, "unstable": 2, "marginal": 3,
                 "unsupported": 4, "invalid": 5}

_SPAN_CACHE: dict = {"v": None}


def data_span(conn) -> dict:
    """回测样本的真实时间边界。必须对外披露——288 个交易日的结论不能冒充长期结论。"""
    if _SPAN_CACHE["v"]:
        return _SPAN_CACHE["v"]
    try:
        r = conn.execute("SELECT MIN(date),MAX(date),COUNT(DISTINCT date) "
                         "FROM stock_indicators_daily").fetchone()
        _SPAN_CACHE["v"] = {"start": r[0], "end": r[1], "days": r[2] or 0}
    except Exception:  # noqa: BLE001
        _SPAN_CACHE["v"] = {"start": "", "end": "", "days": 0}
    return _SPAN_CACHE["v"]


def _rows(conn, sql: str, args=()) -> list:
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def payload(conn) -> dict:
    """前端只读接口。绝不现算 —— 回测在调度线程/离进程里跑。"""
    try:
        ensure_schema(conn)
    except Exception:  # noqa: BLE001
        log.exception("[oss] 建表失败")
    try:
        rules = _rows(conn, "SELECT * FROM oss_rule")
        repos = _rows(conn, "SELECT * FROM oss_repo ORDER BY stars DESC")
    except Exception:  # noqa: BLE001 —— 表不可读时必须降级，不能把异常抛给请求线程
        log.exception("[oss] 读表失败")
        return {"ok": True, "available": False, "asof": ASOF,
                "reason": "开源情报表不可读（可能尚未初始化）",
                "repos": [], "rules": [], "stats": {}, "data_span": data_span(conn)}
    if not rules:
        return {"ok": True, "available": False, "asof": ASOF,
                "reason": "尚未跑过回测（等 oss_daily 调度或手动触发）",
                "repos": repos, "rules": [], "stats": {}}

    rules.sort(key=lambda r: (_STATUS_ORDER.get(r.get("status") or "invalid", 9),
                              -(abs(r.get("excess") or 0))))
    cnt: dict = {}
    for r in rules:
        cnt[r.get("status") or "invalid"] = cnt.get(r.get("status") or "invalid", 0) + 1

    # 头条：优先挑「被推翻且幅度最大」的 —— 这才是有信息量的那条
    head = None
    ref = [r for r in rules if r.get("status") in ("refuted", "unstable")
           and r.get("excess") is not None]
    if ref:
        head = sorted(ref, key=lambda r: -abs(r.get("excess") or 0))[0]

    last = max((r.get("tested_at") or "") for r in rules) or ""
    return {
        "ok": True, "available": True, "asof": ASOF, "tested_at": last,
        "data_span": data_span(conn),
        "stats": {
            "repos": len(repos),
            # 未分级仓库数：扫描是自动的、分级是人工/模型判断的，两者要分开看，
            # 否则「候选 23 个」会被误读成「已消化 23 个」。
            "pending": sum(1 for x in repos if (x.get("bucket") or "?") in ("?", "")),
            "rules": len(rules),
            "active": cnt.get("active", 0),
            "refuted": cnt.get("refuted", 0),
            "unstable": cnt.get("unstable", 0),
            "marginal": cnt.get("marginal", 0),
            "unsupported": cnt.get("unsupported", 0),
            "invalid": cnt.get("invalid", 0),
        },
        "headline": ({"title": head.get("title"), "repo": head.get("repo"),
                      "excess": head.get("excess"), "t": head.get("t"),
                      "status": head.get("status"), "rule": head.get("rule")}
                     if head else None),
        "rules": rules,
        "repos": repos,
    }


def text_block(conn, limit: int = 6) -> str:
    """给 AI 复盘的上下文：开源社区流行、但已被我们数据推翻的说法。

    这一段是为了让模型「知道自己哪条经验不成立」—— 与 evidence.py 同一个初衷。
    只写 refuted / unstable / marginal，不写 active（active 进规则，不进提示词）。
    """
    try:
        ensure_schema(conn)
        rows = _rows(conn, "SELECT title,rule,repo,status,excess,t,n,days,note "
                           "FROM oss_rule WHERE status IN ('refuted','unstable','marginal') "
                           "AND excess IS NOT NULL ORDER BY ABS(excess) DESC LIMIT ?", (limit,))
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return ""
    lab = {"refuted": "反向成立", "unstable": "不稳定", "marginal": "显著但无利可图"}
    out = ["【开源社区流行规则·本系统实测结果（禁止当理由使用）】",
           "下列说法来自 GitHub 上公开的量化项目，已用本项目严格口径（盘后知情、次日收盘建仓、"
           "减全市场等权、t 按日聚合）重测，结论与流行说法不符或不可用："]
    for r in rows:
        out.append("· %s（%s）：%s → T+5 超额 %+.2f%%，t=%.2f，%d 样本/%d 日 → %s"
                   % (r["title"], r["repo"] or "开源", r["rule"],
                      r["excess"] or 0, r["t"] or 0, r["n"] or 0, r["days"] or 0,
                      lab.get(r["status"], r["status"])))
    out.append("→ 因此不得以「均线多头」「突破放量」「超买回调」这类说法作为看多/看空理由，"
               "除非它出现在上面的 active 规则里。")
    sp = data_span(conn)
    if sp.get("days"):
        yrs = (sp.get("days") or 0) / 244.0
        out.append("口径边界：以上主结果样本为 %s ~ %s（%d 个交易日，约 %.1f 年）。"
                   "⚠️ 2026-10-31 起因子表已回补到 2016-01-04，主结果不再只有 14 个月；"
                   "但迁移前的旧结论是用 288 个交易日算出来的，与长样本可能不同 —— "
                   "凡是 tested_at 早于 2026-10-31 的行，一律以「长样本」列的复核为准。"
                   % (sp.get("start"), sp.get("end"), sp.get("days"), yrs))
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "app.db")
    c = sqlite3.connect(db)
    print(refresh(c, force=True, scan=False))
    for r in _rows(c, "SELECT key,status,n,days,excess,t,stable FROM oss_rule ORDER BY key"):
        print("%-22s %-12s n=%-7s days=%-4s ex=%-8s t=%-7s stable=%s"
              % (r["key"], r["status"], r["n"], r["days"], r["excess"], r["t"], r["stable"]))
