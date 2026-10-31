# -*- coding: utf-8 -*-
"""人物「历史战绩」评分引擎。

设计目标（对应王总的需求）：不要只给「出现次数」，而要给出**这个人的可核验战绩**，
并按一个 0~100 的分数从高到低排序，帮助快速判断「谁值得关注」。

为什么这么设计（关键取舍，先讲清楚，避免误读）：

1) **分档而非一刀切**。同一张表里的人，证据强度天差地别：
   - A 档「实测」：在 trades（交割单级）或 positions（持仓）里有记录 —— 是「真出手了」；
   - B 档「提及」：没有交易记录，但帖子**带时间**且**点名了股票**（可核验他当时看好/看空了谁）；
   - C 档「数据不足」：只有无时间、无标指向的帖子 —— **不硬凑分**。
   「说过」≠「买了」，所以 A 档满分 100，B 档上限压到 60，C 档直接 score=NULL。
   绝不用活跃度给 C 档凑一个假分数丢进 A/B 的排序里。

2) **A 档五维度**（权重见顶部常量 W_A_*）：
   实测收益 40 / 记录完整度 20 / 时间跨度 15 / 来源可信 15 / 被跟随度 10。

3) **B 档三维度**（权重见 W_B_*）：提及后 5/20 日胜率 35 / 提及次数 15 / 活跃度 10，
   总分封顶 B_CAP=60。

4) **非自然人识别**：资讯号/机构号（正则见 RE_INFO_ACCOUNT：资讯|客户端|财经|快讯|
   研究|官方|见闻|时报|日报|晚报|周刊|新闻|传媒|证券报|基金报|智库|观察|证券|券商）、
   系统ID（^(股友|用户)\\w*\\d{3,}$）、匿名 都不和真人混在一起排。策略是：仍保留其
   **数据分档**（保证三档人数与整体口径一致），但打 is_natural_person=0 + person_kind
   标记，并由 **in_ranking=0** 在数据层显式表达「不参与排名」——这样任何脚本直接读
   person_scores 也不会把资讯号排进榜，而不是只靠前端把它渲染成「—」。

5) **行情复用而不另造轮子**：涨跌幅一律用项目根 strategy_sim.fetch_kline（新浪日线，
   不复权）+ 本地 kline_cache.json 缓存。抓不到行情时**如实降级**：该维度按已有样本折算，
   并在 score_detail_json 里写清「行情缺失 N 个标的」，不假装算全了。

6) **幂等**：--rebuild 全量重算（先 DELETE 再 INSERT），重复运行结果一致。

CLI：
    python viewer/person_score.py --rebuild          # 重建全量评分 + 导入公开战绩
    python viewer/person_score.py --show 沈飞股票涨了嘛  # 看某人明细
    python viewer/person_score.py --export           # 只导出 CSV（exports/*.csv）
    python viewer/person_score.py --no-net           # 不联网抓行情（仅用本地缓存，快）
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- 路径 / 依赖
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
sys.path.insert(0, BASE)
DB = os.environ.get("VIEWER_DB") or os.path.join(BASE, "viewer", "app.db")
SEED = os.path.join(BASE, "public_records_seed.json")
EXPORT_DIR = os.path.join(BASE, "exports")

# 复用已有行情模块（新浪日线 + 本地缓存），避免另造轮子
from strategy_sim import GAP, fetch_kline, load_kcache, save_kcache  # noqa: E402

# ---------------------------------------------------------------- 可配置权重
# A 档（满分 100）
W_A_RETURN = 40.0          # 实测收益：操作标的之后 5/20 日胜率与平均涨跌
W_A_COMPLETE = 20.0        # 记录完整度：交割单 > 持仓 > 无
W_A_SPAN = 15.0            # 时间跨度：首末记录跨度，越长越能说明稳定性
W_A_TRUST = 15.0           # 来源可信：淘股吧实盘/交割单 > 雪球组合 > 集思录晒单 > 讨论区
W_A_FOLLOW = 10.0          # 被跟随度：其标的后续是否上龙虎榜（数据不足给中位数）
# B 档（满分 B_CAP）
W_B_WIN = 35.0             # 提及后 5/20 日胜率
W_B_MENTION = 15.0         # 提及次数
W_B_ACTIVE = 10.0          # 活跃度（活跃天数）
B_CAP = 60.0               # B 档总分红线：证据弱于 A，不能和 A 平起平坐

# 时间跨度满分对应的天数（约半年），超过即封顶
SPAN_FULL_DAYS = 180.0
# 「被跟随度」数据不足时给的中位数分（同时是满分的 50%）
FOLLOW_MEDIAN = W_A_FOLLOW / 2.0
# 活跃度满分对应的活跃天数
ACTIVE_FULL_DAYS = 8.0
# 提及次数满分对应的次数（≥ 即满分）
MENTION_FULL = 10.0

# 来源可信分档（key 为 posts/trades/positions 里的 site 值）
SITE_TRUST = {
    "taoguba": 15.0,   # 淘股吧：实盘/交割单/有公开赛成绩
    "xueqiu": 11.0,    # 雪球：组合可核验
    "jisilu": 10.0,    # 集思录：晒单
    "moer": 10.0,      # 摩尔金融：组合晒单
    "ths": 6.0,        # 同花顺：讨论区
    "eastmoney": 4.0,  # 东方财富：讨论区
    "sina": 4.0,       # 新浪股吧：讨论区
}
SITE_TRUST_DEFAULT = 4.0  # 未知来源按讨论区对待

# 非自然人识别正则。
# 为什么扩词（2026-09-26，QA 复核）：原词表漏掉了纯媒体号「华尔街见闻」（xueqiu，
# is_natural_person 被误判为 1，落进「真人·数据不足」区）。补入媒体/机构品牌词。
# 两次踩坑的取舍：
#   - **纳入 `证券|券商`**：精准命中 17 个形如「首创证券伍彩云」的**券商/投顾机构账号**
#     （已核对 persons 全表，含「证券」的名字无一例外都是机构账号，无误伤真人）。
#   - **故意不纳入裸词 `银行|基金`**：会误伤散户昵称「基金经理吃双方」「复利银行6666666」
#     （均为 eastmoney、无交易记录，看着就是真人网名）；而「工商银行资讯/平安银行资讯」等
#     已由 `资讯` 命中，无需 `银行`。宁可少抓一个机构号，也不误伤真人。
#   - 同样未纳入过于泛化的 `数据`（未来易误伤「数据帝」类昵称）。
RE_INFO_ACCOUNT = re.compile(
    r"资讯|客户端|财经|快讯|研究|官方|见闻|时报|日报|晚报|周刊|新闻|传媒|证券报|基金报"
    r"|智库|观察|证券|券商"
)
# 系统ID：形如「股友0f6v670753」「用户7540663510」的批量注册账号，非真人昵称
RE_SYSTEM_ID = re.compile(r"^(股友|用户)\w*\d{3,}$")

KLINE_BARS = 300           # 单只标的取最近 300 根日线（约 1.2 年，足够 2026-08/09 事件 + 20 日前瞻）
MAX_FETCH = 90             # 一次重建最多联网抓多少只，防止无界请求


# ---------------------------------------------------------------- 小工具
def _clamp(x: float, lo: float, hi: float) -> float:
    """把分数夹到 [lo, hi]。"""
    return max(lo, min(hi, x))


def _norm_date(s: str | None) -> str:
    """把各种时间戳归一成 'YYYY-MM-DD'；空/异常返回 ''。"""
    if not s:
        return ""
    t = str(s).strip()
    if len(t) < 10:
        return ""
    return t[:10]


def _person_kind(name: str) -> str:
    """判定条目性质：真人 / 资讯号 / 系统ID / 匿名。"""
    n = (name or "").strip()
    if RE_INFO_ACCOUNT.search(n):
        return "资讯号"
    if RE_SYSTEM_ID.match(n):
        return "系统ID"
    if n == "匿名":
        return "匿名"
    return "真人"


def _normalize_nick(s: str) -> str:
    """淘股吧昵称归一化：去 @ 前缀、去首尾空白、去零宽字符。"""
    n = (s or "").strip().lstrip("@").replace("\u200b", "")
    return n


# ---------------------------------------------------------------- 行情与前瞻收益
class KlineStore:
    """按需提供日线的只读封装：优先命中本地缓存，缺失才联网（带节流与降级）。"""

    def __init__(self, allow_net: bool = True) -> None:
        self.cache = load_kcache()
        self.allow_net = allow_net
        self.fetched = 0
        self.missing: set[str] = set()      # 有事件但拿不到行情的标的
        self._rows: dict[str, list[dict]] = {}
        self._dates: dict[str, list[str]] = {}

    def _usable(self, code: str) -> bool:
        ck = self.cache.get(code)
        return bool(ck and ck.get("rows"))

    def ensure(self, codes: set[str]) -> None:
        """把给定代码中有事件、但本地无行情的，联网补齐（有上限、有节流）。"""
        need = [c for c in sorted(codes) if c and not self._usable(c)]
        if not need:
            return
        if not self.allow_net:
            self.missing.update(need)
            return
        for i, code in enumerate(need[:MAX_FETCH], 1):
            try:
                rows = fetch_kline(code, KLINE_BARS)
            except Exception:  # noqa: BLE001 —— 网络异常一律降级，不中断整体评分
                rows = []
            if len(rows) >= 20:
                self.cache[code] = {"rows": rows, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                self.fetched += 1
            else:
                self.missing.add(code)
            time.sleep(GAP)
        for code in need[MAX_FETCH:]:
            self.missing.add(code)
        if self.fetched:
            save_kcache(self.cache)

    def _load(self, code: str) -> None:
        if code in self._rows:
            return
        ck = self.cache.get(code)
        rows = (ck or {}).get("rows") or []
        rows = [r for r in rows if r.get("d") and r.get("c")]
        self._rows[code] = rows
        self._dates[code] = [r["d"] for r in rows]

    def forward_return(self, code: str, date: str, n: int) -> float | None:
        """date 之后第 n 个交易日的涨跌幅（日线收盘价，不复权）。拿不到返回 None。"""
        d = _norm_date(date)
        if not d or not code:
            return None
        self._load(code)
        rows, dates = self._rows[code], self._dates[code]
        if len(rows) <= n:
            return None
        i = bisect.bisect_left(dates, d)   # 首个 >= date 的交易日（date 非交易日则顺延）
        j = i + n
        if i >= len(rows) or j >= len(rows):
            return None
        base = rows[i]["c"]
        if not base or base <= 0:
            return None
        return rows[j]["c"] / base - 1.0


# ---------------------------------------------------------------- 数据装载
def load_person_context(con: sqlite3.Connection) -> dict:
    """把评分所需的原始数据一次性读进内存（739 人、千级记录，规模很小）。"""
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731

    persons = [dict(r) for r in q("SELECT name, mention_count, role FROM persons")]

    # 每人：帖子（含时间、来源站点）
    posts_by: dict[str, list[dict]] = {}
    for r in q("SELECT author, site, post_time FROM posts WHERE author IS NOT NULL"):
        posts_by.setdefault(r["author"], []).append(
            {"site": r["site"] or "", "time": _norm_date(r["post_time"])})

    # 每人：带时间且点名股票的提及（B 档核心、A 档兜底）
    mentions_by: dict[str, list[dict]] = {}
    for r in q("""SELECT p.author AS author, sm.symbol AS symbol, p.post_time AS post_time
                  FROM stock_mentions sm JOIN posts p ON p.id = sm.post_id_ref
                  WHERE p.author IS NOT NULL AND sm.symbol IS NOT NULL AND sm.symbol <> ''
                    AND p.post_time IS NOT NULL AND p.post_time <> ''"""):
        d = _norm_date(r["post_time"])
        if d:
            mentions_by.setdefault(r["author"], []).append({"symbol": r["symbol"], "date": d})

    # 每人：交易记录（成交时间优先 post_time，其次 buy_time / sell_time）
    trades_by: dict[str, list[dict]] = {}
    for r in q("""SELECT author, site, symbol, post_time, buy_time, sell_time
                  FROM trades WHERE author IS NOT NULL"""):
        ts = _norm_date(r["post_time"]) or _norm_date(r["buy_time"]) or _norm_date(r["sell_time"])
        trades_by.setdefault(r["author"], []).append(
            {"symbol": r["symbol"] or "", "date": ts, "site": r["site"] or ""})

    # 每人：持仓（时间用 post_time，注意源数据 buy_time/hold_since 全空）
    positions_by: dict[str, list[dict]] = {}
    for r in q("""SELECT player, site, symbol, post_time
                  FROM positions WHERE player IS NOT NULL"""):
        positions_by.setdefault(r["player"], []).append(
            {"symbol": r["symbol"] or "", "date": _norm_date(r["post_time"]), "site": r["site"] or ""})

    # 龙虎榜标的集合（用于「被跟随度」的真实计算）
    lhb_codes = {r["security_code"] for r in q(
        "SELECT DISTINCT security_code FROM lhb_stocks WHERE security_code IS NOT NULL")}

    return {
        "persons": persons,
        "posts_by": posts_by,
        "mentions_by": mentions_by,
        "trades_by": trades_by,
        "positions_by": positions_by,
        "lhb_codes": lhb_codes,
    }


# ---------------------------------------------------------------- 分维度打分
def _win_rate(rets: list[float]) -> float | None:
    """胜率（涨=胜），空样本返回 None。"""
    if not rets:
        return None
    return sum(1 for x in rets if x > 0) / len(rets)


def _avg(rets: list[float]) -> float | None:
    if not rets:
        return None
    return sum(rets) / len(rets)


def _returns_for_events(kline: KlineStore, events: list[dict]) -> dict:
    """对一组 (symbol,date) 事件算 5/20 日前瞻涨跌，返回聚合结果。

    未取到行情的事件被记入 missing 并跳过（不参与胜率统计），
    这样维度分数反映的是「可核验样本」而非「拍脑袋」。
    """
    r5, r20, used, miss = [], [], [], 0
    best = None
    for ev in events:
        sym, date = ev["symbol"], ev["date"]
        if not sym or not date:
            miss += 1
            continue
        a = kline.forward_return(sym, date, 5)
        b = kline.forward_return(sym, date, 20)
        if a is None and b is None:
            miss += 1
            continue
        used.append(ev)
        if a is not None:
            r5.append(a)
        if b is not None:
            r20.append(b)
        rank = b if b is not None else (a if a is not None else None)
        if rank is not None and (best is None or rank > best["ret"]):
            best = {"ret": rank, "symbol": sym, "date": date}
    return {"r5": r5, "r20": r20, "used": used, "missing": miss, "best": best}


def score_a(person: dict, ctx: dict, kline: KlineStore) -> dict:
    """A 档：五维度打分。返回 (score, detail)。"""
    name = person["name"]
    trades = ctx["trades_by"].get(name, [])
    positions = ctx["positions_by"].get(name, [])
    sites = {t["site"] for t in trades if t["site"]} | {p["site"] for p in positions if p["site"]}
    sites |= {p["site"] for p in ctx["posts_by"].get(name, []) if p["site"]}

    # ---- 维度 1：实测收益（40）----
    # 优先用「成交/持仓时间」做事件；若源数据完全没有时间（本项目多数 trades 无时间），
    # 退而用本人**带时间的提及**估算，并在 basis 里说明——诚实标注证据来源。
    events = [{"symbol": t["symbol"], "date": t["date"]} for t in trades]
    events += [{"symbol": p["symbol"], "date": p["date"]} for p in positions]
    events = [e for e in events if e["symbol"] and e["date"]]
    basis = "交割单/持仓时间"
    if not events:
        events = list(ctx["mentions_by"].get(name, []))
        basis = "以本人带时间的提及估算（源数据无成交时间）"
    agg = _returns_for_events(kline, events)

    wr = _win_rate(agg["r20"]) if agg["r20"] else _win_rate(agg["r5"])
    ar = _avg(agg["r20"]) if agg["r20"] else _avg(agg["r5"])
    if wr is None or ar is None:
        ret_score = 0.0
        ret_note = "无可核验样本（无行情或无时间），该维度计 0"
    else:
        # 胜率映射 0~1 → 0~20；平均涨跌映射 -10%~+10% → 0~20（超出封顶）
        win_pts = _clamp(wr, 0.0, 1.0) * (W_A_RETURN / 2)
        ret_pts = _clamp((ar + 0.10) / 0.20, 0.0, 1.0) * (W_A_RETURN / 2)
        ret_score = win_pts + ret_pts
        ret_note = (f"{'20日' if agg['r20'] else '5日'}胜率 {(wr * 100):.0f}%"
                    f" · 均涨 {ar * 100:+.1f}% · 样本 {len(agg['used'])} 笔")

    # ---- 维度 2：记录完整度（20）----
    if trades:
        complete = W_A_COMPLETE
        complete_note = f"有交割单级记录 {len(trades)} 条"
    elif positions:
        complete = W_A_COMPLETE / 2
        complete_note = f"仅持仓披露 {len(positions)} 条（无成交明细）"
    else:
        complete = 0.0
        complete_note = "无交易/持仓记录"

    # ---- 维度 3：时间跨度（15）----
    all_dates = [t["date"] for t in trades if t["date"]] + [p["date"] for p in positions if p["date"]]
    if len(all_dates) >= 1:
        first, last = min(all_dates), max(all_dates)
        try:
            span = (datetime.strptime(last, "%Y-%m-%d") - datetime.strptime(first, "%Y-%m-%d")).days
        except ValueError:
            span = 0
        span_score = _clamp(span / SPAN_FULL_DAYS, 0.0, 1.0) * W_A_SPAN
        span_note = f"{first} ~ {last}（{span} 天）"
    else:
        span_score, span_note = 0.0, "无带时间的记录"

    # ---- 维度 4：来源可信（15）----
    if sites:
        trust = max(SITE_TRUST.get(s, SITE_TRUST_DEFAULT) for s in sites)
        trust_note = "来源：" + "/".join(sorted(sites))
    else:
        trust = SITE_TRUST_DEFAULT
        trust_note = "来源未知，按讨论区计"

    # ---- 维度 5：被跟随度（10）----
    symbols = {t["symbol"] for t in trades if t["symbol"]} | {p["symbol"] for p in positions if p["symbol"]}
    if symbols and ctx["lhb_codes"]:
        hit = len(symbols & ctx["lhb_codes"])
        follow = _clamp(hit / len(symbols), 0.0, 1.0) * W_A_FOLLOW
        follow_real = True
        follow_note = f"{hit}/{len(symbols)} 个标的登上龙虎榜"
    else:
        follow = FOLLOW_MEDIAN
        follow_real = False
        follow_note = "龙虎榜/标的不足，取中位数分（未真实算出）"

    dims = [
        {"key": "real_return", "label": "实测收益", "score": round(ret_score, 1),
         "max": W_A_RETURN, "detail": ret_note, "real": True},
        {"key": "completeness", "label": "记录完整度", "score": round(complete, 1),
         "max": W_A_COMPLETE, "detail": complete_note, "real": True},
        {"key": "span", "label": "时间跨度", "score": round(span_score, 1),
         "max": W_A_SPAN, "detail": span_note, "real": True},
        {"key": "trust", "label": "来源可信", "score": round(trust, 1),
         "max": W_A_TRUST, "detail": trust_note, "real": True},
        {"key": "follow", "label": "被跟随度", "score": round(follow, 1),
         "max": W_A_FOLLOW, "detail": follow_note, "real": follow_real},
    ]
    total = sum(d["score"] for d in dims)
    notes = []
    if agg["missing"]:
        notes.append(f"行情/时间缺失 {agg['missing']} 个事件，未计入胜率")
    if kline.missing:
        notes.append(f"本轮有 {len(kline.missing)} 个标的未取到行情，相关维度已如实降级")
    detail = {"tier": "A", "basis": basis, "dimensions": dims,
              "sample": {"trades": len(trades), "positions": len(positions), "events": len(events)},
              "notes": notes}
    return _clamp(total, 0.0, 100.0), detail, agg


def score_b(person: dict, ctx: dict, kline: KlineStore) -> dict:
    """B 档：三 维度打分，总分封顶 B_CAP。"""
    name = person["name"]
    mentions = ctx["mentions_by"].get(name, [])
    agg = _returns_for_events(kline, mentions)

    wr = _win_rate(agg["r20"]) if agg["r20"] else _win_rate(agg["r5"])
    if wr is None:
        win_score, win_note = 0.0, "无行情样本，该维度计 0"
    else:
        win_score = _clamp(wr, 0.0, 1.0) * W_B_WIN
        win_note = f"{'20日' if agg['r20'] else '5日'}胜率 {(wr * 100):.0f}% · 样本 {len(agg['used'])} 笔"

    mc = int(person.get("mention_count") or 0)
    men_score = _clamp(mc / MENTION_FULL, 0.0, 1.0) * W_B_MENTION

    posts = ctx["posts_by"].get(name, [])
    active_days = len({p["time"] for p in posts if p["time"]})
    act_score = _clamp(active_days / ACTIVE_FULL_DAYS, 0.0, 1.0) * W_B_ACTIVE

    dims = [
        {"key": "win", "label": "提及后胜率", "score": round(win_score, 1),
         "max": W_B_WIN, "detail": win_note, "real": wr is not None},
        {"key": "mention", "label": "提及次数", "score": round(men_score, 1),
         "max": W_B_MENTION, "detail": f"提及 {mc} 次", "real": True},
        {"key": "active", "label": "活跃度", "score": round(act_score, 1),
         "max": W_B_ACTIVE, "detail": f"活跃 {active_days} 天", "real": True},
    ]
    total = _clamp(sum(d["score"] for d in dims), 0.0, B_CAP)
    notes = [f"B 档上限 {B_CAP:.0f}：仅有『提及』证据，弱于 A 档实测"]
    if agg["missing"]:
        notes.append(f"行情缺失 {agg['missing']} 个提及，未计入胜率")
    detail = {"tier": "B", "basis": "本人带时间的股票提及", "dimensions": dims,
              "cap": B_CAP, "notes": notes}
    return total, detail, agg


def score_c(person: dict) -> dict:
    """C 档：数据不足，score=NULL，绝不硬凑分。"""
    return None, {"tier": "C", "basis": "无带时间的帖子、也无标指向",
                  "dimensions": [], "notes": ["数据不足，不参与评分（不用活跃度凑分）"]}, None


# ---------------------------------------------------------------- 公开战绩
def load_public_records(con: sqlite3.Connection) -> dict:
    """把 public_records_seed.json 里的真实抓取结果导入 public_records 表。

    为什么用种子文件：一次性 WebFetch 的结果必须是**可复现**的——把它落成 JSON，
    重建时直接导入，既保证幂等，也留痕（哪条来自文章、哪条来自搜索快照）。
    """
    con.executescript("""
        CREATE TABLE IF NOT EXISTS public_records (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          nickname TEXT, platform TEXT, group_name TEXT,
          total_return REAL, as_of_date TEXT, source_url TEXT,
          source_kind TEXT, fetched_at TEXT,
          UNIQUE(nickname, platform, group_name, as_of_date)
        );
    """)
    if not os.path.exists(SEED):
        return {"imported": 0, "matched": 0, "by_name": {}, "issues": 0}
    try:
        payload = json.load(open(SEED, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print("  [warn] 读取公开战绩种子失败:", str(e)[:80], flush=True)
        return {"imported": 0, "matched": 0, "by_name": {}, "issues": 0}

    records = payload.get("records") or []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con.execute("DELETE FROM public_records")
    by_name: dict[str, list[dict]] = {}
    for r in records:
        nick = _normalize_nick(r.get("nickname"))
        if not nick or r.get("total_return") is None:
            continue
        con.execute(
            """INSERT OR IGNORE INTO public_records
               (nickname, platform, group_name, total_return, as_of_date, source_url, source_kind, fetched_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (nick, r.get("platform") or "", r.get("group") or "",
             float(r["total_return"]), r.get("as_of") or "",
             r.get("source_url") or "", r.get("source_kind") or "", now))
        by_name.setdefault(nick, []).append({
            "platform": r.get("platform") or "",
            "group": r.get("group") or "",
            "total_return": float(r["total_return"]),
            "as_of": r.get("as_of") or "",
            "source_url": r.get("source_url") or "",
            "source_kind": r.get("source_kind") or "",
        })

    # 每人取「总收益最高的一条」排序（仅用于统计/留痕，不再往人物表回填）
    for nick in by_name:
        by_name[nick].sort(key=lambda x: -x["total_return"])
    con.commit()
    # 口径如实（QA 复核）：按**交易日**（as_of_date）去重计数，而不是按 (platform, as_of) 对
    # ——后者会把「官方年赛 + 非凡杯」同日两条算成两期，虚报期数（曾误报「10 期」）。
    day_count = len({r.get("as_of") for r in records if r.get("as_of")})
    return {"imported": len(records), "by_name": by_name, "days": day_count}


# （已移除）曾有的 pick_public_record(nick, by_name)：
#   它把「本库人物 ↔ 公开年赛榜」按昵称硬匹配，写进 person_scores.public_record_json。
#   2026-09-26 QA 复核确认：两者**零交集**（竞赛账号 vs 本库抓取的新浪/东财用户），
#   该函数对 739 行全部返回 None，public_record_json 恒为 NULL，前端的公开战绩分支是死代码。
#   故删除这段「注定为空的耦合」。person_scores.public_record_json 列**保留**（结构稳定、
#   前端字段不破），但当前恒为 NULL；将来若真能匹配上公开战绩，再把写入逻辑加回来。
#   公开年赛榜改为在页面上单列「外部参照」区块（server.api_public_records），不再往人物表里塞。


# ---------------------------------------------------------------- 主流程
def build(allow_net: bool = True) -> dict:
    """全量重建 person_scores。返回统计信息。"""
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        ctx = load_person_context(con)

        # 先确定每人分档与事件，再一次性把需要的标的行情补齐（避免逐个触发网络）
        need_codes: set[str] = set()
        tiers: dict[str, str] = {}
        for person in ctx["persons"]:
            name = person["name"]
            has_measure = bool(ctx["trades_by"].get(name)) or bool(ctx["positions_by"].get(name))
            if has_measure:
                tiers[name] = "A"
                for t in ctx["trades_by"].get(name, []):
                    if t["symbol"] and t["date"]:
                        need_codes.add(t["symbol"])
                for p in ctx["positions_by"].get(name, []):
                    if p["symbol"] and p["date"]:
                        need_codes.add(p["symbol"])
            elif ctx["mentions_by"].get(name):
                tiers[name] = "B"
                for m in ctx["mentions_by"][name]:
                    need_codes.add(m["symbol"])
            else:
                tiers[name] = "C"

        kline = KlineStore(allow_net=allow_net)
        kline.ensure(need_codes)

        pub = load_public_records(con)
        by_name = pub.get("by_name", {})

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS person_scores (
              name TEXT PRIMARY KEY,
              score REAL, score_tier TEXT,
              is_natural_person INTEGER, person_kind TEXT,
              in_ranking INTEGER,
              mention_count INTEGER, post_count INTEGER, timed_mention_count INTEGER,
              trade_count INTEGER, position_count INTEGER,
              first_seen TEXT, last_seen TEXT, active_days INTEGER, symbol_count INTEGER,
              win_rate_5d REAL, win_rate_20d REAL, avg_ret_5d REAL, avg_ret_20d REAL,
              best_call_ret REAL, best_call_symbol TEXT, best_call_date TEXT,
              evidence_level TEXT, score_detail_json TEXT, public_record_json TEXT,
              computed_at TEXT
            );
        """)
        # 老库（person_scores 已存在但缺 in_ranking 列）：CREATE IF NOT EXISTS 不会补列，需 ALTER。
        # 幂等：列已存在时 ALTER 抛错，吞掉即可。
        try:
            con.execute("ALTER TABLE person_scores ADD COLUMN in_ranking INTEGER")
        except sqlite3.OperationalError as e:  # P2-02：精确吞「列已存在」，其余 OperationalError 照常抛出
            if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                pass
            else:
                raise
        con.execute("DELETE FROM person_scores")

        tier_counts = {"A": 0, "B": 0, "C": 0}
        rows_out: list[dict] = []

        for person in ctx["persons"]:
            name = person["name"]
            tier = tiers[name]
            posts = ctx["posts_by"].get(name, [])
            mentions = ctx["mentions_by"].get(name, [])
            trades = ctx["trades_by"].get(name, [])
            positions = ctx["positions_by"].get(name, [])

            # 性质判定 + 「实盘优先」覆盖（2026-09-26 QA 复核）：
            # 名字命中媒体/机构词表时默认判为非自然人；但**只要有 ≥1 条结构化持仓记录，一律视为自然人**
            # ——有实盘可追踪，与「只搬运资讯、没有个人交易」的资讯号/客户端/系统ID 本质上是两类。
            # 名字可能起得像机构（如「人类超短研究所」，淘股吧账号、带 5 条真实持仓），但数据说明它是选手。
            # 注意：**只认 positions，不认 trades**——8 个资讯号/系统ID 各有 1 条 trades（抓取噪音），
            # 放宽到 trades 会把「新浪财经客户端」等又放回真人榜。
            kind = _person_kind(name)
            overridden = False
            if kind != "真人" and positions:
                kind = "真人"
                overridden = True

            if tier == "A":
                score, detail, agg = score_a(person, ctx, kline)
            elif tier == "B":
                score, detail, agg = score_b(person, ctx, kline)
            else:
                score, detail, agg = score_c(person)

            detail["data_tier"] = tier
            if overridden:
                detail["person_kind"] = "真人"
                detail.setdefault("notes", []).append(
                    f"名字命中媒体/机构词表，但有 {len(positions)} 条实盘持仓记录，"
                    f"按「实盘优先」判为真人（{_person_kind(name)} → 真人）")
            elif kind != "真人":
                detail["person_kind"] = kind
                detail.setdefault("notes", []).append(
                    f"非自然人（{kind}），已标记、不占真人榜单前排")

            all_times = [p["time"] for p in posts if p["time"]] + \
                        [t["date"] for t in trades if t["date"]] + \
                        [p["date"] for p in positions if p["date"]]
            first_seen = min(all_times) if all_times else ""
            last_seen = max(all_times) if all_times else ""
            active_days = len(set(all_times))
            symbol_count = len({t["symbol"] for t in trades if t["symbol"]} |
                               {p["symbol"] for p in positions if p["symbol"]} |
                               {m["symbol"] for m in mentions if m["symbol"]})

            agg = agg or {}
            wr5 = _win_rate(agg.get("r5") or [])
            wr20 = _win_rate(agg.get("r20") or [])
            av5 = _avg(agg.get("r5") or [])
            av20 = _avg(agg.get("r20") or [])
            best = agg.get("best")

            # 证据等级：交割单 > 组合/晒单 > 仅讨论
            if trades:
                evidence = "交割单"
            elif positions:
                sites = {p["site"] for p in positions if p["site"]}
                evidence = "晒单" if sites & {"jisilu", "moer"} else "组合"
            elif tier == "B":
                evidence = "仅讨论"
            else:
                evidence = ""

            # 非自然人 = in_ranking 0（数据层显式表态），真人 = 1。
            # score 数值本身**不动**：避免分数跳动、并保住「score NULL 只在 C 档」的验收口径。
            in_ranking = 1 if kind == "真人" else 0

            tier_counts[tier] += 1
            rows_out.append({
                "name": name, "score": round(score, 1) if score is not None else None,
                "score_tier": tier, "is_natural_person": 1 if kind == "真人" else 0,
                "person_kind": kind, "in_ranking": in_ranking,
                "mention_count": int(person.get("mention_count") or 0),
                "post_count": len(posts), "timed_mention_count": len(mentions),
                "trade_count": len(trades), "position_count": len(positions),
                "first_seen": first_seen, "last_seen": last_seen,
                "active_days": active_days, "symbol_count": symbol_count,
                "win_rate_5d": round(wr5, 4) if wr5 is not None else None,
                "win_rate_20d": round(wr20, 4) if wr20 is not None else None,
                "avg_ret_5d": round(av5, 4) if av5 is not None else None,
                "avg_ret_20d": round(av20, 4) if av20 is not None else None,
                "best_call_ret": round(best["ret"], 4) if best else None,
                "best_call_symbol": best["symbol"] if best else "",
                "best_call_date": best["date"] if best else "",
                "evidence_level": evidence,
                "score_detail_json": json.dumps(detail, ensure_ascii=False),
                # 恒为 None：本库人物与公开年赛榜零交集（见上方说明）；列保留以备未来接入
                "public_record_json": None,
                "computed_at": now,
            })

        con.executemany(
            """INSERT OR REPLACE INTO person_scores
               (name, score, score_tier, is_natural_person, person_kind, in_ranking,
                mention_count, post_count, timed_mention_count, trade_count, position_count,
                first_seen, last_seen, active_days, symbol_count,
                win_rate_5d, win_rate_20d, avg_ret_5d, avg_ret_20d,
                best_call_ret, best_call_symbol, best_call_date,
                evidence_level, score_detail_json, public_record_json, computed_at)
               VALUES (:name,:score,:score_tier,:is_natural_person,:person_kind,:in_ranking,
                :mention_count,:post_count,:timed_mention_count,:trade_count,:position_count,
                :first_seen,:last_seen,:active_days,:symbol_count,
                :win_rate_5d,:win_rate_20d,:avg_ret_5d,:avg_ret_20d,
                :best_call_ret,:best_call_symbol,:best_call_date,
                :evidence_level,:score_detail_json,:public_record_json,:computed_at)""",
            rows_out)
        con.commit()

        write_csv(rows_out)
        pub_rows = [dict(r) for r in con.execute(
            "SELECT nickname, platform, group_name, total_return, as_of_date, source_url, source_kind "
            "FROM public_records ORDER BY platform, as_of_date, total_return DESC")]
        write_public_csv(pub_rows)

        return {
            "tiers": tier_counts,
            "natural": sum(1 for r in rows_out if r["is_natural_person"] == 1),
            "non_natural": sum(1 for r in rows_out if r["is_natural_person"] == 0),
            "in_ranking": sum(1 for r in rows_out if r["in_ranking"] == 1),
            "public_imported": pub.get("imported", 0),
            "public_days": pub.get("days", 0),
            "public_names": len(by_name),
            "kline_fetched": kline.fetched,
            "kline_missing": len(kline.missing),
        }
    finally:
        con.close()


# ---------------------------------------------------------------- CSV 导出
CSV_FIELDS = ["name", "score", "score_tier", "is_natural_person", "person_kind", "in_ranking",
              "mention_count", "post_count", "timed_mention_count", "trade_count",
              "position_count", "first_seen", "last_seen", "active_days", "symbol_count",
              "win_rate_5d", "win_rate_20d", "avg_ret_5d", "avg_ret_20d",
              "best_call_ret", "best_call_symbol", "best_call_date", "evidence_level"]


def write_csv(rows: list[dict]) -> str:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    path = os.path.join(EXPORT_DIR, "persons_score.csv")
    # utf-8-sig：带 BOM，Excel 双击不乱码
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def write_public_csv(rows: list[dict]) -> str:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    path = os.path.join(EXPORT_DIR, "public_records.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["nickname", "platform", "group_name", "total_return",
                           "as_of_date", "source_url", "source_kind"], extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


# ---------------------------------------------------------------- CLI
def show_person(name: str) -> None:
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM person_scores WHERE name=?", (name,)).fetchone()
    if not r:
        # 模糊提示
        like = con.execute("SELECT name FROM person_scores WHERE name LIKE ? LIMIT 10",
                           (f"%{name}%",)).fetchall()
        print(f"未找到「{name}」。相近：{[x['name'] for x in like]}")
        con.close()
        return
    d = dict(r)
    print(f"姓名：{d['name']}    分档：{d['score_tier']}    评分：{d['score']}")
    print(f"性质：{d['person_kind']}（自然人={d['is_natural_person']}，"
          f"参与排名 in_ranking={d.get('in_ranking')}）  证据：{d['evidence_level']}")
    print(f"发帖 {d['post_count']} · 被提及 {d['mention_count']} · 交易 {d['trade_count']} · 持仓 {d['position_count']}")
    print(f"活跃：{d['first_seen']} ~ {d['last_seen']}（{d['active_days']} 天）  覆盖标的 {d['symbol_count']}")
    print(f"胜率：5日 {d['win_rate_5d']} / 20日 {d['win_rate_20d']}   均涨：5日 {d['avg_ret_5d']} / 20日 {d['avg_ret_20d']}")
    print("维度明细：")
    try:
        det = json.loads(d["score_detail_json"] or "{}")
    except Exception:  # noqa: BLE001
        det = {}
    for dim in det.get("dimensions", []):
        flag = "" if dim.get("real", True) else "（降级/未真实算出）"
        print(f"  - {dim['label']:<10} {dim['score']:>5} / {dim['max']:<4}  {dim['detail']}{flag}")
    print("依据：", det.get("basis", ""))
    for n in det.get("notes", []):
        print("  注：", n)
    if d["public_record_json"]:
        print("公开战绩：", d["public_record_json"])
    else:
        print("公开战绩：无（本库人物与公开年赛榜暂无交集，该字段恒为 NULL）")
    con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="人物历史战绩评分引擎")
    ap.add_argument("--rebuild", action="store_true", help="全量重算并落库")
    ap.add_argument("--show", metavar="NAME", help="查看某人的评分明细")
    ap.add_argument("--export", action="store_true", help="仅导出 CSV")
    ap.add_argument("--no-net", action="store_true", help="不联网抓行情（仅用本地缓存）")
    args = ap.parse_args()

    if args.show:
        show_person(args.show)
        return 0
    if args.export:
        con = sqlite3.connect(DB, timeout=30)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM person_scores")]
        con.close()
        if rows:
            print("已导出：", write_csv(rows))
        else:
            print("person_scores 为空，请先 --rebuild")
        return 0

    t0 = time.time()
    print("[person_score] 开始重建…", flush=True)
    stats = build(allow_net=not args.no_net)
    print(f"  分档人数：A={stats['tiers']['A']} B={stats['tiers']['B']} C={stats['tiers']['C']}")
    print(f"  自然人 {stats['natural']} / 非自然人 {stats['non_natural']}"
          f"（其中参与排名 in_ranking=1 的 {stats['in_ranking']} 人）")
    print(f"  行情：本轮新抓 {stats['kline_fetched']} 只，缺失 {stats['kline_missing']} 只")
    print(f"  公开战绩：导入 {stats['public_imported']} 条，覆盖 {stats['public_days']} 个交易日，"
          f"涉及 {stats['public_names']} 个昵称（与本库 persons 零交集，仅作外部参照）")
    print(f"  CSV：{os.path.join(EXPORT_DIR, 'persons_score.csv')}")
    print(f"  完成，用时 {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
