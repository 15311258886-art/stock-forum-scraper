# -*- coding: utf-8 -*-
"""多智能体辩论研究（阶段二）——把「单角色一次性问答」升级为「投研团队 + 多空辩论 + 风控审单」。

## 为什么要有这个模块（与 ai_review 的分工）

`ai_review.py` 是**一个交易员、一次调用、单一视角**：把整包信息喂给一个 system prompt，
直接吐一份票单。它快、便宜，但本质上仍是「一个模型的一个看法」，用户原话是"没那么智能"。

本模块走另一条路：**只对阶段一（用户已有的内置筛选逻辑）产出的候选池做深度研究**，
用多个角色分工 + 对抗式辩论，逼出单一模型看不到的另一面。对应抖音那条视频里
TradingAgents-CN / AlphaEar 的核心玩法：分析师各查一块 → 看涨 vs 看跌研究员辩论 →
交易员出票单 → 风控审单。

## 铁律：绝不扫全市场

5000+ 只逐只辩论 = 一轮 3~4 万次 LLM 调用，成本与延迟都不可能。所以分两段：

    阶段一（已有，无 LLM）：stock_universe_scan / stock_signals / stock_strategy_picks
        → 候选池 N 只（默认 50）
    阶段二（本模块，多智能体）：候选池 → 分析师 → 辩论 → 票单 → 风控 → 终推 M 只

阶段一的表全在 `viewer/app.db`，本模块只读不写，不改变你现有筛选逻辑的任何行为。

## 两种形态

- `debate_research()`  —— 形态 A：盘后批量。候选池 50 → 终推 15~18。
- `single_stock_research()` —— 形态 B：单股/主题深研。只对 1 只跑同一套辩论链。

## 成本与并发

一次批量约 50(分析师) + 30(辩论) + 20(票单) + 1(风控) ≈ 101 次调用。
LLM 调用是**网络等待**，所以分析师/辩论/票单三个阶段用线程池并行（默认 4 并发）。
**读库全部在主线程完成**，工作线程只发 HTTP、不碰 sqlite —— 避免跨线程共享连接。

## 免责

所有输出仅供学习研究，不构成投资建议；LLM 采样有随机性，同一票两次结论可能不同。
（与 ai_review 的口径一致，前端须照实展示。）
"""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 旋钮（env 可覆盖）

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except Exception:  # noqa: BLE001
        return default


# 候选池 / 辩论池 / 票单池 / 终推区间 / 并发
POOL = _env_int("DEBATE_POOL", 50)
DEBATE_TOP = _env_int("DEBATE_TOP", 30)
PLAN_TOP = _env_int("DEBATE_PLAN_TOP", 20)
FINAL_MIN = _env_int("DEBATE_FINAL_MIN", 15)
FINAL_MAX = _env_int("DEBATE_FINAL_MAX", 18)
CONCURRENCY = _env_int("DEBATE_CONCURRENCY", 4)
# 单次批量的调用上限保险丝（防参数被调飞导致账单失控）
MAX_CALLS = _env_int("DEBATE_MAX_CALLS", 400)

DISCLAIMER = "AI 多智能体研究，仅供学习参考，不构成投资建议；同一标的两次结论可能不同。"


def _ar():
    """延迟 import ai_review（复用它的 call_llm / parse_output / validate_picks / 建表口径）。"""
    from viewer import ai_review as _m
    return _m


# ---------------------------------------------------------------- 小工具


def _q(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception as e:  # noqa: BLE001
        log.debug("debate 取数失败 %s: %s", sql[:60], e)
        return []


def _one(conn, sql, args=()):
    rows = _q(conn, sql, args)
    return rows[0][0] if rows else None


def _row(conn, table: str, where: str, args=()):
    """按表名取一行并转 dict。表名是内部常量，非用户输入（无注入面）。"""
    try:
        cur = conn.execute("SELECT * FROM %s WHERE %s" % (table, where), args)
        cols = [d[0] for d in cur.description]
        r = cur.fetchone()
        return dict(zip(cols, r)) if r else None
    except Exception:  # noqa: BLE001
        return None


def _num(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt(v, nd=2, suffix=""):
    try:
        if v is None or v == "":
            return "—"
        return ("%." + str(nd) + "f") % float(v) + suffix
    except Exception:  # noqa: BLE001
        return "—"


def _clip(s, n):
    if s is None:
        return ""
    t = " ".join(str(s).split())
    return t if len(t) <= n else t[:n] + "…"


# ---------------------------------------------------------------- 建表

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_debate (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date  TEXT,
    provider     TEXT,
    mode         TEXT,          -- batch / single
    run_key      TEXT,          -- single 时为 6 位代码，batch 为空
    status       TEXT,          -- ok / err
    pool         INTEGER,       -- 候选池规模
    produced     INTEGER,       -- 终推只数
    result_json  TEXT,          -- 终稿（picks + 辩论摘要 + 风控）
    trace_json   TEXT,          -- 每只票的中间过程（可审计 AI 读了什么、怎么吵的）
    calls        INTEGER,       -- 本轮 LLM 调用次数
    elapsed_ms   INTEGER,
    err          TEXT,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_ai_debate ON ai_debate(mode, run_key, created_at DESC);
"""

_ensured = False


def ensure_tables(conn) -> None:
    global _ensured
    if _ensured:
        return
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        _ensured = True
    except Exception:  # noqa: BLE001
        log.exception("ai_debate 建表失败")


def _reset_ensured() -> None:  # 测试用
    global _ensured
    _ensured = False


# ---------------------------------------------------------------- 阶段一：候选池（只读）


def _is_bad_name(name: str) -> bool:
    n = (name or "").upper()
    return "ST" in n or "退" in n


def _suspended(conn, code: str) -> bool:
    try:
        return bool(_one(conn, "SELECT 1 FROM suspended WHERE code=?", (code,)))
    except Exception:  # noqa: BLE001
        return False


def select_candidates(conn, pool: int = POOL) -> list:
    """从阶段一结果里取候选池。多级兜底：universe_scan → indicators → snapshot。

    只读，不做任何写库与打分改变。返回 [{code,name,scan,pre}]，按阶段一排序取前 pool。
    """
    pool = max(1, int(pool or POOL))
    rows = _q(
        conn,
        "SELECT code,name,score,rank_pct,tier,momentum,volume,quality "
        "FROM stock_universe_scan WHERE tier IN ('A','B','C') "
        "ORDER BY rank_pct DESC LIMIT ?", (pool * 4,))
    if not rows:
        rows = _q(
            conn,
            "SELECT code,name,score,rank_pct,tier,momentum,volume,quality "
            "FROM stock_universe_scan ORDER BY rank_pct DESC LIMIT ?", (pool * 4,))
    if not rows:
        # 兜底：还没跑全宇宙扫描时，用因子表按量比排序
        rows = _q(
            conn,
            "SELECT code,name,NULL,NULL,NULL,NULL,NULL,NULL FROM stock_indicators "
            "ORDER BY volume_ratio DESC LIMIT ?", (pool * 4,))

    out = []
    for r in rows:
        code = str(r[0] or "").strip()
        name = str(r[1] or "").strip()
        if not code:
            continue
        if _is_bad_name(name):
            continue
        if _suspended(conn, code):
            continue
        out.append({
            "code": code,
            "name": name,
            "scan": {"score": r[2], "rank_pct": r[3], "tier": r[4],
                     "momentum": r[5], "volume": r[6], "quality": r[7]},
        })
        if len(out) >= pool:
            break
    return out


# ---------------------------------------------------------------- 每票数据切片


def stock_brief(conn, code: str) -> dict:
    """把一只票的可用数据拼成切片。全表缺失项一律留空，交给渲染层写「数据缺失」。"""
    ind = _row(conn, "stock_indicators", "code=?", (code,)) or {}
    sig = _row(conn, "stock_signals", "code=?", (code,)) or {}
    scan = _row(conn, "stock_universe_scan", "code=?", (code,)) or {}
    val = _row(conn, "stock_daily_snapshot", "code=? ORDER BY trade_date DESC LIMIT 1", (code,)) or {}
    fund = _row(conn, "fundamentals", "symbol=?", (code,)) or {}
    mentions = 0
    try:
        mentions = int(_one(conn, "SELECT COUNT(*) FROM stock_mentions WHERE symbol=?", (code,)) or 0)
    except Exception:  # noqa: BLE001
        mentions = 0
    tpl = []
    for r in _q(conn, "SELECT template,score FROM stock_strategy_picks WHERE code=?", (code,)):
        tpl.append({"template": r[0], "score": r[1]})

    name = (ind.get("name") or val.get("name") or fund.get("name")
            or scan.get("name") or "")
    if not any([ind, sig, scan, val, fund]):
        return {}
    return {"code": code, "name": name, "ind": ind, "sig": sig,
            "scan": scan, "val": val, "fund": fund, "mentions": mentions, "templates": tpl}


def _render_brief(b: dict, ctx: dict | None = None) -> str:
    ind, val, fund, sig, scan = b.get("ind") or {}, b.get("val") or {}, \
        b.get("fund") or {}, b.get("sig") or {}, b.get("scan") or {}
    L = ["【标的】%s(%s)" % (b.get("name") or "—", b.get("code"))]
    if ctx and ctx.get("market_line"):
        L.append("【市场背景】" + ctx["market_line"])

    # 估值 / 基本面
    L.append("【估值/基本面】PE(TTM)=%s，PB=%s，总市值=%s，ROE=%s，毛利率=%s，"
             "营收同比=%s，净利同比=%s"
             % (_fmt(val.get("pe_ttm")), _fmt(val.get("pb")),
                _fmt(_num(val.get("total_mv")) / 1e8, 1, "亿") if val.get("total_mv") else "—",
                _fmt(fund.get("roe")), _fmt(fund.get("gm")),
                _fmt(fund.get("rev_yoy"), 1, "%"), _fmt(fund.get("np_yoy"), 1, "%")))
    # 量价 / 技术
    L.append("【量价/技术】收盘=%s，MA20=%s，MA60=%s，MACD=%s/柱=%s，RSI6=%s，"
             "ATR=%s，量比=%s，BIAS12=%s，ADX=%s，Supertrend=%s"
             % (_fmt(ind.get("close")), _fmt(ind.get("ma20")), _fmt(ind.get("ma60")),
                _fmt(ind.get("macd")), _fmt(ind.get("macdh")), _fmt(ind.get("rsi6")),
                _fmt(ind.get("atr")), _fmt(ind.get("volume_ratio")),
                _fmt(ind.get("bias12")), _fmt(ind.get("adx")), _fmt(ind.get("supertrend"))))
    if val.get("pct") is not None or val.get("turnover") is not None:
        L.append("【当日】涨跌=%s，换手=%s，成交额=%s"
                 % (_fmt(val.get("pct"), 2, "%"), _fmt(val.get("turnover"), 2, "%"),
                    _fmt(_num(val.get("amount")) / 1e8, 2, "亿") if val.get("amount") else "—"))
    # 阶段一打分
    if scan:
        L.append("【阶段一打分】综合=%s，百分位=%s，档位=%s，动量=%s，量能=%s，质量=%s，理由=%s"
                 % (_fmt(scan.get("score"), 3), _fmt(scan.get("rank_pct"), 3),
                    scan.get("tier") or "—", _fmt(scan.get("momentum"), 3),
                    _fmt(scan.get("volume"), 3), _fmt(scan.get("quality"), 3),
                    _clip(scan.get("reasons"), 80) or "—"))
    # 信号
    if sig:
        L.append("【买卖信号】买入分=%s，卖出分=%s，买入理由=%s，卖出理由=%s"
                 % (_fmt(sig.get("buy_score")), _fmt(sig.get("sell_score")),
                    _clip(sig.get("buy_reasons"), 60) or "—",
                    _clip(sig.get("sell_reasons"), 60) or "—"))
    # 策略模板命中
    if b.get("templates"):
        L.append("【策略模板命中】" + "、".join(
            "%s(%s)" % (t.get("template"), _fmt(t.get("score"), 2)) for t in b["templates"]))
    # 舆情热度
    L.append("【论坛舆情】相关提及 %d 条" % int(b.get("mentions") or 0))
    return "\n".join(L)


# ---------------------------------------------------------------- 提示词


ANALYST_SYSTEM = """你是 A 股投研团队的首席分析师。只依据给定数据，从四个视角独立判断这一只票，
不许编造数据里没有的信息（缺失就写「数据缺失」）。

只输出 JSON：
{
  "fundamental": {"stance": "偏多/中性/偏空", "points": ["最多2条，带数字"], "score": 0-100},
  "sentiment":   {"stance": "偏多/中性/偏空", "points": ["最多2条"], "score": 0-100},
  "technical":   {"stance": "偏多/中性/偏空", "points": ["最多2条"], "score": 0-100},
  "sector":      {"stance": "偏多/中性/偏空", "points": ["最多2条"], "score": 0-100},
  "pre_score": 0-100,
  "key_risk": "一句话，20字内"
}

视角口径：
- fundamental：估值(PE/PB/市值)与基本面(ROE/毛利率/营收增速/净利增速)。
- sentiment：论坛提及热度与情绪倾向（只有条数就只依据条数，别编情绪）。
- technical：均线/MACD/RSI/量比/ATR/BIAS/ADX/Supertrend。
- sector：结合市场背景判断所处板块阶段与资金。
pre_score 是四视角综合看好度（越高越看好）。只输出 JSON，无任何前后缀。"""


DEBATE_SYSTEM = """你是投研辩论主持人。给定一只票的分析师四视角观点，让「看涨研究员」和「看跌研究员」
各打一轮，逼出最强多头理由与最强空头理由，然后由你裁定。

只输出 JSON：
{
  "bull_case": ["最强看多理由，最多3条，尽量带数据"],
  "bear_case": ["最强看空理由，最多3条，尽量带数据"],
  "clash": [{"point": "分歧点", "bull": "多头怎么说", "bear": "空头怎么说"}],
  "judge": {"stance": "看好/中性/回避", "adjusted_score": 0-100, "confidence": 0-100,
            "one_line": "20字内裁定"}
}

要求：不许和稀泥；bear_case 必须真的去找风险（估值/技术背离/情绪过热/基本面瑕疵），
找不到就说明「未发现显著空头逻辑」。只输出 JSON。"""


PLAN_SYSTEM = """你是 A 股交易员。基于分析师观点与多空辩论结论，为这一只票给一张可执行票单。
只输出 JSON：
{
  "symbol": "6位代码", "name": "名称",
  "score": 0-100,
  "score_breakdown": {"trend": 0-25, "money": 0-25, "position": 0-25, "catalyst": 0-25},
  "action": "买/等回踩/观察",
  "entry_low": 数值, "entry_high": 数值, "stop": 数值, "target": 数值, "horizon": 5,
  "thesis": "30字内", "why_now": "40字内",
  "evidence": ["带数字的可核对事实", "至少2条"],
  "invalidate": "30字内"
}

硬约束（不满足会被机检整条作废）：
- score 必须等于 score_breakdown 四项之和；四项各 0~25。
- entry_low < entry_high；stop < entry_low；target > entry_high；四个价都 > 0 且落在当前价附近可执行范围。
- evidence 至少 2 条且必须带数字。
- 不许出现「占位/待补/示例」。不许编数据里没有的代码/事件。
- 评不到 60 分的票，action 写「观察」，score 照实，不要硬推。
只输出 JSON。"""


RISK_SYSTEM = """你是组合风控。给定一批待推荐票单与用户交易纪律，逐票审核是否违反纪律、
是否估值离谱、是否情绪过热或逻辑牵强，并给出组合层面的提醒。

只输出 JSON：
{
  "approved": [
    {"symbol": "6位代码", "name": "名称",
     "decision": "通过/降级/否决", "reason": "20字内"}
  ],
  "portfolio_note": "80字内，组合集中度/仓位/风险提醒"
}

判断口径：与交易纪律冲突、逻辑只靠情绪撑、估值明显透支的，给「降级」或「否决」；
其余「通过」。只输出 JSON。"""


# ---------------------------------------------------------------- LLM 调用（可 monkeypatch）


def _usage(res) -> dict:
    u = (res or {}).get("usage") or {}
    return {"prompt": int(u.get("prompt_tokens") or 0),
            "completion": int(u.get("completion_tokens") or 0)}


def _ask(provider: str, system: str, user: str, *, retries: int = 1):
    """调一次模型并解析 JSON。返回 (dict, usage)。失败返回 ({}, usage含err)。"""
    ar = _ar()
    last = {"prompt": 0, "completion": 0, "err": "not called"}
    for _ in range(retries + 1):
        res = ar.call_llm(provider, system, user)
        if res.get("ok"):
            out = ar.parse_output(res.get("text") or "")
            if isinstance(out, dict) and out:
                return out, _usage(res)
            last = dict(_usage(res), err="unparseable")
        else:
            last = dict(_usage(res), err=res.get("err") or "call failed")
    return {}, last


def _parallel(fn, items, workers: int):
    if not items:
        return []
    w = max(1, min(int(workers or 1), len(items)))
    if w == 1:
        return [fn(x) for x in items]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=w) as ex:
        return list(ex.map(fn, items))


# ---------------------------------------------------------------- 背景信息（复用 ai_review 上下文）


def _backdrop(conn, date: str, provider: str) -> dict:
    """取一小段市场背景 + 用户交易纪律 + 持仓，供提示词引用。失败一律降级为空。"""
    ar = _ar()
    ctx = {}
    try:
        ctx = ar.build_context(conn, date, provider) or {}
    except Exception:  # noqa: BLE001
        ctx = {}
    market = ctx.get("market") or {}
    reg = market.get("regime") or {}
    breadth = market.get("breadth") or {}
    try:
        market_line = "%s；全市场涨%d跌%d，涨停%d只，等权涨幅%+.2f%%" % (
            reg.get("state") or "状态未知",
            int(breadth.get("up") or 0), int(breadth.get("down") or 0),
            int(breadth.get("limit_up") or 0), _num(breadth.get("avg_pct")))
    except Exception:  # noqa: BLE001
        market_line = ""
    doctrine = ctx.get("doctrine") or {}
    if isinstance(doctrine, dict):
        doctrine_text = doctrine.get("text") or ""
    else:
        doctrine_text = str(doctrine)
    return {"market_line": market_line, "doctrine_text": _clip(doctrine_text, 2000),
            "portfolio": ctx.get("portfolio")}


# ---------------------------------------------------------------- 渲染辩论/票单/风控输入


def _render_debate_input(b: dict, ctx: dict) -> str:
    return (_render_brief(b, ctx) + "\n\n【分析师四视角】\n"
            + json.dumps(b.get("analyst") or {}, ensure_ascii=False))


def _render_plan_input(b: dict, ctx: dict) -> str:
    return (_render_brief(b, ctx)
            + "\n\n【分析师四视角】\n" + json.dumps(b.get("analyst") or {}, ensure_ascii=False)
            + "\n\n【多空辩论】\n" + json.dumps(b.get("debate") or {}, ensure_ascii=False))


def _render_risk_input(picks: list, ctx: dict) -> str:
    lines = []
    if ctx.get("doctrine_text"):
        lines.append("【用户交易纪律】\n" + ctx["doctrine_text"])
    if ctx.get("portfolio"):
        try:
            lines.append("【当前持仓】\n" + json.dumps(ctx["portfolio"], ensure_ascii=False)[:1200])
        except Exception:  # noqa: BLE001
            pass
    lines.append("【待审票单】")
    for p in picks:
        lines.append("- %s %s 评分%s 动作%s｜%s" % (
            p.get("symbol"), p.get("name") or "—",
            _fmt(p.get("score"), 0), p.get("action") or "—",
            _clip(p.get("thesis"), 30)))
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程：形态 A 批量


def _make_verdict(final: list, ctx: dict) -> str:
    if not final:
        return "本轮无票通过风控，空仓等待更好机会。"
    strong = sum(1 for p in final if _num(p.get("score")) >= 70)
    return "候选池经多空辩论后精选 %d 只（其中强票 %d 只）；%s" % (
        len(final), strong, _clip(ctx.get("market_line") or "", 40))


def _persist(conn, date, provider, mode, run_key, status, pool, produced,
             result, trace, calls, elapsed_ms, err=""):
    try:
        conn.execute(
            "INSERT INTO ai_debate(review_date,provider,mode,run_key,status,pool,produced,"
            "result_json,trace_json,calls,elapsed_ms,err,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (date, provider, mode, run_key or "", status, int(pool or 0), int(produced or 0),
             json.dumps(result, ensure_ascii=False) if result else None,
             json.dumps(trace, ensure_ascii=False) if trace else None,
             int(calls or 0), int(elapsed_ms or 0), err or None,
             time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("ai_debate 落库失败")
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass


def _resolve_provider(provider: str) -> str:
    ar = _ar()
    provider = (provider or "").strip()
    allowed = [k for k, p in ar.PROVIDERS.items() if k != "workbuddy" and p.get("url")]
    if provider in allowed and ar.provider_key(provider):
        return provider
    for k in allowed:
        if ar.provider_key(k):
            return k
    return ar.DEFAULT_PROVIDER


def debate_research(conn, date: str = "", provider: str = "", pool: int = POOL,
                    debate_top: int = DEBATE_TOP, plan_top: int = PLAN_TOP,
                    final_min: int = FINAL_MIN, final_max: int = FINAL_MAX,
                    concurrency: int = CONCURRENCY) -> dict:
    """形态 A：候选池 → 分析师 → 多空辩论 → 票单 → 风控 → 终推。

    **同步阻塞**，调用方负责放到后台线程。返回结果同时落 ai_debate。
    """
    ensure_tables(conn)
    ar = _ar()
    date = (date or "").strip() or ar.latest_trade_date(conn)
    provider = _resolve_provider(provider)
    if not ar.provider_key(provider):
        return {"ok": False, "error": "未配置大模型 API Key：请在主面板设置里填入 DeepSeek / 混元 / 千问任一，再跑辩论。"}
    pool = max(1, int(pool or POOL))
    final_min = max(0, int(final_min if final_min is not None else FINAL_MIN))
    final_max = max(final_min, int(final_max or FINAL_MAX))
    t0 = time.time()
    calls = [0]

    def acc(u):
        calls[0] += 1

    cands = select_candidates(conn, pool)
    if not cands:
        res = {"mode": "batch", "review_date": date, "provider": provider,
               "picks": [], "note": "候选池为空（stock_universe_scan / 指标表无数据）",
               "disclaimer": DISCLAIMER}
        _persist(conn, date, provider, "batch", "", "err", 0, 0, res, None, 0,
                 int((time.time() - t0) * 1000), "empty_pool")
        return {"ok": False, "error": "empty_pool", "data": res}

    # 1) 建切片（主线程读库，工作线程不碰 sqlite）
    briefs = []
    for c in cands:
        b = stock_brief(conn, c["code"])
        if b:
            if not b.get("name"):
                b["name"] = c.get("name") or ""
            briefs.append(b)
    if not briefs:
        res = {"mode": "batch", "review_date": date, "provider": provider,
               "picks": [], "note": "候选池无可用数据切片",
               "disclaimer": DISCLAIMER}
        _persist(conn, date, provider, "batch", "", "err", len(cands), 0, res, None, 0,
                 int((time.time() - t0) * 1000), "no_brief")
        return {"ok": False, "error": "no_brief", "data": res}

    ctx = _backdrop(conn, date, provider)

    # 2) 分析师面板（并行）
    def _analyst(b):
        return _ask(provider, ANALYST_SYSTEM, _render_brief(b, ctx))
    panels = _parallel(_analyst, briefs, concurrency)
    for b, (out, u) in zip(briefs, panels):
        acc(u)
        b["analyst"] = out
        b["pre_score"] = _num(out.get("pre_score"), 50.0)
    # 分析师全失败 → 按阶段一分排序兜底
    if all(not b.get("analyst") for b in briefs):
        for b, c in zip(briefs, cands):
            b["pre_score"] = _num((c.get("scan") or {}).get("score"), 50.0) * 100

    briefs.sort(key=lambda x: x["pre_score"], reverse=True)
    short = briefs[:min(max(1, debate_top), len(briefs))]
    short = short[:max(1, min(len(short), MAX_CALLS - calls[0]))] if calls[0] < MAX_CALLS else []

    # 3) 多空辩论（并行）
    def _debate(b):
        return _ask(provider, DEBATE_SYSTEM, _render_debate_input(b, ctx))
    douts = _parallel(_debate, short, concurrency)
    for b, (out, u) in zip(short, douts):
        acc(u)
        b["debate"] = out
        js = (out.get("judge") or {}) if isinstance(out, dict) else {}
        b["adj_score"] = _num(js.get("adjusted_score"), b["pre_score"])

    short.sort(key=lambda x: x.get("adj_score", 0), reverse=True)
    plan_set = short[:min(max(1, plan_top), len(short))]
    if calls[0] >= MAX_CALLS:
        plan_set = []

    # 4) 交易票单（并行）
    def _plan(b):
        return _ask(provider, PLAN_SYSTEM, _render_plan_input(b, ctx))
    pouts = _parallel(_plan, plan_set, concurrency)
    raw_picks = []
    for b, (out, u) in zip(plan_set, pouts):
        acc(u)
        if not isinstance(out, dict) or not out.get("symbol"):
            continue
        # 代码以候选为准，防止模型张冠李戴
        out["symbol"] = b["code"]
        if not out.get("name"):
            out["name"] = b.get("name") or ""
        raw_picks.append(out)

    # 5) 机检（复用 ai_review.validate_picks），只留干净的
    valid = []
    rejected = []
    for p in raw_picks:
        chk = ar.validate_picks({"picks": [p]})
        if chk.get("clean"):
            valid.append(p)
        else:
            rejected.append({"symbol": p.get("symbol"), "issues": chk.get("bad")})
    valid.sort(key=lambda p: _num(p.get("score")), reverse=True)
    valid = valid[:final_max]

    # 6) 风控审单（一次调用）
    risk = {}
    if valid and calls[0] < MAX_CALLS:
        r_out, r_u = _ask(provider, RISK_SYSTEM, _render_risk_input(valid, ctx))
        acc(r_u)
        risk = r_out if isinstance(r_out, dict) else {}

    decisions = {}
    for d in (risk.get("approved") or []):
        if isinstance(d, dict) and d.get("symbol"):
            decisions[str(d["symbol"])] = d
    final = []
    for p in valid:
        d = decisions.get(str(p.get("symbol")))
        if d and d.get("decision") == "否决":
            continue
        if d and d.get("decision") == "降级":
            p["risk_flag"] = "降级：" + _clip(d.get("reason") or "", 20)
        final.append(p)
    final = final[:final_max]

    result = {
        "mode": "batch",
        "review_date": date,
        "provider": provider,
        "pool": len(cands),
        "briefed": len(briefs),
        "shortlist": len(short),
        "planned": len(plan_set),
        "final": len(final),
        "verdict": _make_verdict(final, ctx),
        "picks": final,
        "risk_note": _clip(risk.get("portfolio_note") or "", 120),
        "debate_summary": [
            {"symbol": b["code"], "name": b.get("name") or "",
             "pre_score": round(b.get("pre_score", 0), 1),
             "adj_score": round(b.get("adj_score", b.get("pre_score", 0)), 1),
             "bull": (b.get("debate") or {}).get("bull_case") or [],
             "bear": (b.get("debate") or {}).get("bear_case") or [],
             "judge": (b.get("debate") or {}).get("judge") or {}}
            for b in short],
        "calls": calls[0],
        "note": "" if len(final) >= final_min else
                "有效票单 %d 只，低于目标下限 %d（宁缺毋滥，未凑数）" % (len(final), final_min),
        "disclaimer": DISCLAIMER,
    }
    trace = {
        "candidates": [{"code": c["code"], "name": c.get("name")} for c in cands],
        "briefs": [{"code": b["code"], "name": b.get("name"),
                    "pre_score": b.get("pre_score"),
                    "analyst": b.get("analyst")} for b in briefs],
        "rejected_picks": rejected,
        "risk_raw": risk,
    }
    elapsed = int((time.time() - t0) * 1000)
    _persist(conn, date, provider, "batch", "", "ok" if final else "err",
             len(cands), len(final), result, trace, calls[0], elapsed,
             "" if final else "no_valid_pick")
    return {"ok": True, "data": result, "elapsed_ms": elapsed, "calls": calls[0]}


# ---------------------------------------------------------------- 主流程：形态 B 单股深研


def single_stock_research(conn, code: str, provider: str = "") -> dict:
    """形态 B：只对一只票跑同一套「分析师 → 辩论 → 票单」链，可选风控。"""
    ensure_tables(conn)
    ar = _ar()
    code = str(code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return {"ok": False, "error": "代码须为 6 位数字"}
    provider = _resolve_provider(provider)
    if not ar.provider_key(provider):
        return {"ok": False, "error": "未配置大模型 API Key：请在主面板设置里填入 DeepSeek / 混元 / 千问任一。"}
    date = ar.latest_trade_date(conn)
    t0 = time.time()
    calls = [0]

    b = stock_brief(conn, code)
    if not b:
        return {"ok": False, "error": "没有该代码的指标数据（stock_indicators）"}
    if not b.get("name"):
        r = _row(conn, "stock_universe_scan", "code=?", (code,)) or {}
        b["name"] = r.get("name") or ""
    ctx = _backdrop(conn, date, provider)

    out_a, u1 = _ask(provider, ANALYST_SYSTEM, _render_brief(b, ctx)); calls[0] += 1
    b["analyst"] = out_a
    out_d, u2 = _ask(provider, DEBATE_SYSTEM, _render_debate_input(b, ctx)); calls[0] += 1
    b["debate"] = out_d
    out_p, u3 = _ask(provider, PLAN_SYSTEM, _render_plan_input(b, ctx)); calls[0] += 1
    pick = None
    if isinstance(out_p, dict) and out_p.get("symbol"):
        out_p["symbol"] = code
        if not out_p.get("name"):
            out_p["name"] = b.get("name") or ""
        chk = ar.validate_picks({"picks": [out_p]})
        if chk.get("clean"):
            pick = out_p
        else:
            b["reject"] = chk.get("bad")

    result = {
        "mode": "single", "review_date": date, "provider": provider,
        "code": code, "name": b.get("name") or "",
        "analyst": out_a,
        "debate": out_d,
        "pick": pick,
        "pick_valid": bool(pick),
        "reject": None if pick else (b.get("reject") or [{"issue": "模型未给出有效票单"}]),
        "calls": calls[0],
        "disclaimer": DISCLAIMER,
    }
    elapsed = int((time.time() - t0) * 1000)
    _persist(conn, date, provider, "single", code, "ok" if pick else "err",
             1, 1 if pick else 0, result, {"analyst": out_a, "debate": out_d},
             calls[0], elapsed, "" if pick else "no_valid_pick")
    return {"ok": True, "data": result, "elapsed_ms": elapsed, "calls": calls[0]}


# ---------------------------------------------------------------- 读接口


def latest(conn, mode: str = "batch", run_key: str = "") -> dict:
    """取最近一次结果（前端轮询用）。"""
    ensure_tables(conn)
    row = _row(conn, "ai_debate",
               "mode=? AND run_key=? ORDER BY created_at DESC, id DESC LIMIT 1",
               (mode, run_key or ""))
    if not row:
        return {}
    out = dict(row)
    for k in ("result_json", "trace_json"):
        if out.get(k):
            try:
                out["data" if k == "result_json" else "trace"] = json.loads(out[k])
            except Exception:  # noqa: BLE001
                pass
        out.pop(k, None)
    return out


# ---------------------------------------------------------------- CLI


def _cli():
    import argparse
    from viewer.db import Database
    ap = argparse.ArgumentParser(description="多智能体辩论研究")
    ap.add_argument("--code", default="", help="形态B：六位代码（给了就跑单股）")
    ap.add_argument("--pool", type=int, default=POOL)
    ap.add_argument("--final", type=int, default=FINAL_MIN)
    ap.add_argument("--provider", default="")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = ap.parse_args()
    conn = Database().conn
    if args.code:
        res = single_stock_research(conn, args.code, args.provider)
    else:
        res = debate_research(conn, provider=args.provider, pool=args.pool,
                              final_min=args.final, final_max=max(args.final, FINAL_MAX))
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        d = (res.get("data") or {})
        if res.get("ok"):
            print("== 多智能体辩论研究 ==")
            print("候选池 %s → 终推 %s 只，调用 %s 次，用时 %ss" % (
                d.get("pool"), d.get("final"), d.get("calls"),
                round((res.get("elapsed_ms") or 0) / 1000.0, 1)))
            print(d.get("verdict") or "")
            for p in (d.get("picks") or []):
                print("  %s %s 分%s %s 买%.2f~%.2f 止%.2f 目标%.2f｜%s" % (
                    p.get("symbol"), p.get("name"), p.get("score"), p.get("action"),
                    _num(p.get("entry_low")), _num(p.get("entry_high")),
                    _num(p.get("stop")), _num(p.get("target")), p.get("thesis") or ""))
            if d.get("note"):
                print("注：" + d["note"])
            print(d.get("disclaimer") or "")
        else:
            print("失败：" + str(res.get("error")))


if __name__ == "__main__":
    _cli()
