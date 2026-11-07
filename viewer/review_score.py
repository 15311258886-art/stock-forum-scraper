# -*- coding: utf-8 -*-
"""复盘计分器 —— 让大模型的每条判断都能被事后算账（2026-10-31 建立）。

## 要解决的问题

原来的 `ai_review` 是个**不可被评价的顾问**：它每天写一份判断，然后没有下文。
`vs_last` 段是让模型自己回忆「上次说的兑现了没有」——这是整个链路最大的作弊点：
模型自评命中率天然偏向自我肯定，且它根本没有价格数据去核对。

结果就是：**跑了 5 次复盘，准确率依然是未知数**。一个不被证伪的分析师，
说得越顺越危险。

## 做法

把一次复盘的输出拆成「可计分单元」落库（表 `ai_call`）：

  kind='opp'       opportunities[]        看多个股  → 命中 = T+H 超额 > 0
  kind='avoid'     portfolio_advice[] 减/清  看空个股  → 命中 = T+H 超额 < 0
  kind='market'    market_read.state      市场方向  → 按普涨/普跌/震荡/结构各自判据
  kind='mainline'  mainlines[]            板块方向  → 弱代理，见下

**计分口径与全项目其它探针完全一致**（这点必须严格，否则不同模块的数字没法比）：

  · 复盘针对交易日 d（盘后做）→ 建仓基准是 **d 之后第一个交易日 d1 的收盘**
  · 计分价 = d1 之后第 H 个交易日的收盘（H=5 / 10）
  · 超额 = 个股收益 − 同日全市场等权均值（demean）
  · 只在目标日已经存在收盘价时才回填，否则保持 open（**绝不拿未来数据凑数**）

## 已知局限（写在这里，防止后人高估这个分数）

1. **板块判断的计分是弱代理**。库里没有板块指数序列，`sector_chain` 只有 67 行 /
   十来个交易日，无法算板块前向收益。所以 mainline 的命中用「次日该板块是否仍在
   资金净流入前列」当代理，标记 `proxy=1`，**不进主成绩单的合并命中率**。
2. **样本天然很小**。一天最多 5~8 条机会，要积累到 30 条有效样本至少需要 4~6 周。
   在此之前应当把命中率当「趋势」而不是「结论」——界面上也照此措辞。
3. 只对**有代码**的判断计分。纯文字的风险描述（如「流动性收紧」）无法归因到个股，
   一律 status='void' 并写明原因，不假装能在意。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import statistics
import time

log = logging.getLogger(__name__)

# 2026-11-02 扩展：原来只有 5 / 10 两档 —— 结果是「今天推的明天涨没涨」这个问题
# **在数据结构上就回答不了**：必须等满 5 个交易日才有第一个数字。
# 而 score_pending 里一句 `if not hs: continue` 更把这堵墙砌死：只要 T+5 没到，
# 连 T+1 都不写。于是「预测→次日验证→反思」这个最短回路永远走不通。
# 现在四档并行，哪档到期就先出哪档（见 score_pending 的 any_hit 早退逻辑）。
HORIZONS = (1, 3, 5, 10)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_call (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id     INTEGER,                 -- 关联 ai_review.id
    review_date   TEXT NOT NULL,           -- 复盘针对的交易日
    provider      TEXT NOT NULL,
    kind          TEXT NOT NULL,           -- pick / opp / avoid / market / mainline
    symbol        TEXT,                    -- 个股代码（market/mainline 为空）
    name          TEXT,
    stance        TEXT,                    -- bull / bear / state:<普涨…>
    thesis        TEXT,
    trigger       TEXT,
    invalidate    TEXT,
    -- 荐股专属（2026-11-02）：模型自评的推荐分 0-100 与动作、买入区间、止损目标
    score         INTEGER DEFAULT 0,       -- 推荐分（越高越该买），非 pick 类为 0
    action        TEXT,                    -- 买 / 等回踩 / 观察
    entry_low     REAL, entry_high REAL,   -- 建议买入区间
    stop          REAL,                    -- 止损价
    target        REAL,                    -- 目标价
    horizon_days  INTEGER,                 -- 建议持有交易日
    -- 回填区
    base_date     TEXT,                    -- 建仓基准日 d1（d 之后第一个交易日）
    base_close    REAL,
    n1_date       TEXT,  n1_ret REAL,  n1_ex REAL,  n1_hit INTEGER,
    h1_date       TEXT,  h1_ret REAL,  h1_ex REAL,  h1_hit INTEGER,
    h3_date       TEXT,  h3_ret REAL,  h3_ex REAL,  h3_hit INTEGER,
    h5_date       TEXT,  h5_ret REAL,  h5_ex REAL,  h5_hit INTEGER,
    h10_date      TEXT,  h10_ret REAL, h10_ex REAL, h10_hit INTEGER,
    proxy         INTEGER DEFAULT 0,        -- 1 = 弱代理，不进合并命中率
    status        TEXT NOT NULL DEFAULT 'open',   -- open / scored / void
    void_reason   TEXT,
    scored_at     TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ai_call_date ON ai_call(review_date, kind);
CREATE INDEX IF NOT EXISTS ix_ai_call_stat ON ai_call(status, id);
"""

# 老库补列：(表, 列, 类型)。建表语句只对**新库**生效，已存在的 app.db 不会自动长出新列，
# 而 ALTER TABLE ADD COLUMN 是 SQLite 唯一可用的在线迁移方式（不能改类型、不能加约束）。
_MIGRATE_COLS = (
    ("score", "INTEGER DEFAULT 0"),
    ("action", "TEXT"), ("entry_low", "REAL"), ("entry_high", "REAL"),
    ("stop", "REAL"), ("target", "REAL"), ("horizon_days", "INTEGER"),
    ("h1_date", "TEXT"), ("h1_ret", "REAL"), ("h1_ex", "REAL"), ("h1_hit", "INTEGER"),
    ("h3_date", "TEXT"), ("h3_ret", "REAL"), ("h3_ex", "REAL"), ("h3_hit", "INTEGER"),
    # n1 = 「推荐后第一个交易日」口径：复盘日 d 收盘 → 次一交易日 d1 收盘。
    # 与 h1（d1 收盘建仓 → d2 收盘）不是一回事：n1 回答"我昨晚推的，今天涨没涨"，
    # h1 回答"如果你昨天没追上、今天收盘才买，明天赚没赚"。前者是准不准，
    # 后者是能不能赚到。两个都要，混为一谈就会把"看对了但追不到"算成看错。
    ("n1_date", "TEXT"), ("n1_ret", "REAL"), ("n1_ex", "REAL"), ("n1_hit", "INTEGER"),
)

_ensured = False


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(ai_call)")}
    for col, typ in _MIGRATE_COLS:
        if col not in have:
            try:
                conn.execute("ALTER TABLE ai_call ADD COLUMN %s %s" % (col, typ))
            except Exception:  # noqa: BLE001
                log.exception("[review_score] 补列失败 %s", col)
    conn.commit()


def ensure_schema(conn: sqlite3.Connection) -> None:
    global _ensured
    if _ensured:
        return
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        _ensured = True
    except Exception:  # noqa: BLE001
        log.exception("[review_score] 建表失败")


# ---------------------------------------------------------------- 抽取


_SYM_RE = re.compile(r"^\d{6}$")


def _clip(s, n: int):
    if s is None:
        return ""
    t = re.sub(r"\s+", " ", str(s)).strip()
    return t if len(t) <= n else t[:n] + "…"


def _symbol_of(item: dict, name2code: dict | None) -> str:
    """从条目里取 6 位代码：先看 symbol/code 字段，再按名称反查。"""
    for k in ("symbol", "code", "stock_code"):
        v = str(item.get(k) or "").strip()
        if _SYM_RE.match(v):
            return v
    nm = str(item.get("name") or "").strip()
    if nm and name2code:
        c = name2code.get(nm)
        if c:
            return c
    return ""


def _name2code(conn) -> dict:
    """最新交易日的 名称→代码 映射（用于 portfolio_advice 只给了名称的情况）。"""
    try:
        d = conn.execute("SELECT MAX(trade_date) FROM stock_daily_snapshot").fetchone()[0]
        if not d:
            return {}
        return {r[1]: r[0] for r in conn.execute(
            "SELECT code, name FROM stock_daily_snapshot WHERE trade_date=? AND name IS NOT NULL",
            (d,))}
    except Exception:  # noqa: BLE001
        return {}


def record_from_review(conn, review_id: int, review_date: str, provider: str,
                       output: dict) -> int:
    """把一次复盘的输出拆成 ai_call 行。返回写入条数。

    **去重键 = (复盘日, kind, 代码, 名称, 立场)，不是 review_id。**
    原因：同一天可能因为调参/换模型跑出多份复盘（实测 10-28 一天有 4 份 deepseek）。
    如果按 review_id 去重，同一只票会被登记 4 次，而它们**不是独立样本** ——
    命中率会被同一批票反复计数，样本量虚增、置信区间收窄，等于自己骗自己。
    所以写入前先按"单元"删旧行，保证「一天一个标的只有一个立场」。
    """
    ensure_schema(conn)
    if not isinstance(output, dict):
        return 0
    try:
        conn.execute("DELETE FROM ai_call WHERE review_id=?", (int(review_id),))
    except Exception:  # noqa: BLE001
        pass

    name2code = _name2code(conn)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rows: list = []

    # ① 荐股清单（2026-11-02 起为主口径）—— 带代码、推荐分、动作、买入区间、止损目标。
    #    这是进哥要的东西：直接给票，不给"方向"。没有代码的条目一律不入账
    #    （入账了也没法验证，只会把命中率稀释成假的）。
    picks = [x for x in (output.get("picks") or []) if isinstance(x, dict)]
    picked_syms = set()
    for it in picks[:10]:
        sym = _symbol_of(it, name2code)
        if not sym:
            continue          # 无代码 = 无法计分，宁可不记，也不进统计
        picked_syms.add(sym)
        try:
            sc = int(round(float(it.get("score") or 0)))
        except Exception:  # noqa: BLE001
            sc = 0
        def _f(k):
            try:
                v = it.get(k)
                return float(v) if v not in (None, "") else None
            except Exception:  # noqa: BLE001
                return None
        # 价位不全的推荐**不入账**：实测模型会为凑数量写一只 entry/stop/target 全 0
        # 的"占位票"。它永远触发不了也永远不会被判错，混进分母只会让准确率失真。
        # （页面上仍然会显示这一条并标红 —— 让它看见自己凑数，但不让它污染统计。）
        _lv = [_f("entry_low"), _f("entry_high"), _f("stop"), _f("target")]
        if any(v is None or v <= 0 for v in _lv):
            continue
        rows.append((
            review_id, review_date, provider, "pick", sym,
            _clip(it.get("name"), 20), "bull",
            _clip(it.get("thesis") or it.get("why_now"), 200),
            _clip(it.get("trigger"), 120), _clip(it.get("invalidate"), 120),
            max(0, min(100, sc)), _clip(it.get("action"), 12),
            _f("entry_low"), _f("entry_high"), _f("stop"), _f("target"),
            int(_f("horizon") or 5) if _f("horizon") else 5, 0,
        ))

    # ② 机会（老口径，兼容 picks 之前的历史数据）
    #    若同一只票已经在 picks 里出现，这里跳过 —— 否则同一天同一只票会被记两次，
    #    命中率被自己重复计数（这正是 record_from_review 去重注释里防的那个病）。
    for it in (output.get("opportunities") or [])[:8]:
        if not isinstance(it, dict):
            continue
        sym = _symbol_of(it, name2code)
        # 2026-11-02：**没有代码的机会一律不入账**。
        # 实测 10-28 那批里三条 opp 的 name 分别是「低波池A级标的（98只）」
        # 「半导体设备/材料（自选兆易创新）」「空仓/中证红利」—— 它们不是股票，
        # 永远算不出收益，却一直挂在 status='open' 里，让「待回填 9 条」看起来像
        # 「还在验证中」，实际是**永远不会有结果的死账**。死账比没账更糟：
        # 它让样本数虚高、让分母里塞满不可能命中的东西、也让"准确率"永远算不真。
        if not sym:
            continue
        if sym in picked_syms:
            continue
        rows.append((review_id, review_date, provider, "opp", sym,
                     _clip(it.get("name"), 20), "bull",
                     _clip(it.get("thesis"), 200), _clip(it.get("trigger"), 120),
                     _clip(it.get("invalidate"), 120), 0, "", None, None, None, None, 5, 0))

    # ② 持仓建议里明确看空的（减/清）才计分；加→看多；持/观察 不计分（无方向可验）
    for it in (output.get("portfolio_advice") or [])[:12]:
        if not isinstance(it, dict):
            continue
        adv = str(it.get("advice") or "")
        if adv in ("减", "清", "卖出", "减仓", "清仓"):
            stance = "bear"
        elif adv in ("加", "买入", "加仓"):
            stance = "bull"
        else:
            continue
        sym = _symbol_of(it, name2code)
        if not sym:
            continue      # 同上：无代码的看空建议无法验证（实测「*ST双成」就查不到代码）
        rows.append((review_id, review_date, provider, "avoid", sym,
                     _clip(it.get("name"), 20), stance,
                     _clip(it.get("reason"), 200), "", "", 0, "", None, None, None, None, 5, 0))

    # ③ 市场方向
    mr = output.get("market_read") or {}
    state = str(mr.get("state") or "").strip()
    if state:
        rows.append((review_id, review_date, provider, "market", "", "",
                     "state:%s" % state, _clip(mr.get("logic"), 300), "", "", 0, "", None, None, None, None, 5, 0))

    # ④ 主线板块（弱代理）
    for it in (output.get("mainlines") or [])[:6]:
        if not isinstance(it, dict) or not it.get("sector"):
            continue
        rows.append((review_id, review_date, provider, "mainline", "",
                     _clip(it.get("sector"), 24), "bull",
                     _clip(it.get("logic"), 200), _clip(it.get("stage"), 20), "", 0, "", None, None, None, None, 5, 1))

    if not rows:
        return 0

    try:
        # 按「单元」去重：同一天、同一标的、同一立场只留最后一次
        for r in rows:
            conn.execute(
                "DELETE FROM ai_call WHERE review_date=? AND kind=? AND stance=? "
                "AND COALESCE(symbol,'')=COALESCE(?,'') AND COALESCE(name,'')=COALESCE(?,'')",
                (r[1], r[3], r[6], r[4], r[5]))
        conn.executemany(
            "INSERT INTO ai_call(review_id,review_date,provider,kind,symbol,name,stance,"
            "thesis,trigger,invalidate,score,action,entry_low,entry_high,stop,target,"
            "horizon_days,proxy,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [r + (now,) for r in rows])
        conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("[review_score] 写入失败 review_id=%s", review_id)
        return 0
    return len(rows)


# ---------------------------------------------------------------- 价格工具


def _calendar(conn) -> list:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM stock_indicators_daily ORDER BY date")]


def _closes(conn, codes: list) -> dict:
    """code -> {date: close}。只取需要的代码，避免全表扫。"""
    out: dict = {}
    codes = [c for c in set(codes) if c]
    if not codes:
        return out
    CH = 400  # 分批，避免 SQL 变量上限
    for i in range(0, len(codes), CH):
        part = codes[i:i + CH]
        ph = ",".join("?" * len(part))
        for code, d, c in conn.execute(
                "SELECT code,date,close FROM stock_indicators_daily "
                "WHERE close>0 AND code IN (%s)" % ph, part):
            out.setdefault(code, {})[d] = c
    return out


def _day_stats(conn, dates: list) -> dict:
    """各日全市场等权涨幅（%）+ 涨跌家数。用 close 相对前一交易日，与探针口径一致。

    为什么不用 `stock_daily_snapshot` 取广度：该表只保留了最近两个交易日
    （10480 行），历史计分全部取不到 —— 实测因此 market 类判断长期无法回填。
    日线表 `stock_indicators_daily` 覆盖 2025-08 至今，用它算广度同样准确且更耐久。

    只需覆盖参与计分的那几天，故按日期过滤，不全表扫。
    返回 {date: {"avg": 等权涨幅%, "up": n, "down": n, "total": n}}
    """
    if not dates:
        return {}
    cal = _calendar(conn)
    idx = {d: i for i, d in enumerate(cal)}
    want_prev = set()
    for d in dates:
        i = idx.get(d)
        if i is not None and i > 0:
            want_prev.add(cal[i - 1])
    use = sorted(set(dates) | want_prev)
    if not use:
        return {}
    ph = ",".join("?" * len(use))
    px: dict = {}
    for code, d, c in conn.execute(
            "SELECT code,date,close FROM stock_indicators_daily "
            "WHERE close>0 AND date IN (%s)" % ph, use):
        px.setdefault(d, {})[code] = c
    out: dict = {}
    for d in dates:
        i = idx.get(d)
        if i is None or i == 0:
            continue
        prev = cal[i - 1]
        a, b = px.get(prev) or {}, px.get(d) or {}
        vals = [(b[c] / a[c] - 1.0) * 100.0 for c in b if c in a and a[c] > 0]
        if len(vals) >= 100:
            out[d] = {
                "avg": statistics.fmean(vals),
                "up": sum(1 for v in vals if v > 0),
                "down": sum(1 for v in vals if v < 0),
                "total": len(vals),
            }
    return out


def _hit(kind: str, stance: str, ex: float, mkt_avg: float, breadth: dict | None):
    """判定命中。

    opp / bull  → 超额 > 0 即命中（跑赢市场）
    avoid / bear→ 超额 < 0 即命中（它说要跌，真跌了）
    market      → 按状态各自的判据（见下）
    """
    if kind in ("opp", "mainline"):
        return 1 if ex > 0 else 0
    if kind == "avoid":
        return 1 if ex < 0 else 0
    if kind == "market":
        s = stance.replace("state:", "")
        if s == "普涨":
            return 1 if mkt_avg > 0.3 else 0
        if s == "普跌":
            return 1 if mkt_avg < -0.3 else 0
        if s == "震荡":
            return 1 if abs(mkt_avg) <= 1.0 else 0
        if s == "结构":
            # 结构市 = 涨跌家数明显分化，而整体涨跌幅不大
            if not breadth:
                return None
            up, dn, tot = breadth.get("up", 0), breadth.get("down", 0), breadth.get("total", 0)
            if not tot:
                return None
            return 1 if abs(up - dn) / tot >= 0.25 and abs(mkt_avg) <= 1.5 else 0
        return None
    return None


def score_pending(conn, limit: int = 2000) -> dict:
    """回填所有已到期但未计分的单元。返回统计。"""
    ensure_schema(conn)
    # 死账清理（幂等）：个股类条目没有代码，就永远算不出收益。
    # 不清掉它们，`still_open` 会一直挂着几条永远不动的记录，
    # 界面上看起来像"还在验证中"，实际是没人会再碰的残留。
    try:
        conn.execute(
            "UPDATE ai_call SET status='void', "
            "void_reason='条目没有 6 位代码（写成了池名/板块名，或名称查不到代码），无法按个股验证' "
            "WHERE status='open' AND COALESCE(symbol,'')='' AND kind IN ('pick','opp','avoid')")
        conn.commit()
    except Exception:  # noqa: BLE001
        log.exception("[review_score] 死账清理失败")
    cal = _calendar(conn)
    if not cal:
        return {"scored": 0, "still_open": 0, "voided": 0}
    idx = {d: i for i, d in enumerate(cal)}

    rows = list(conn.execute(
        "SELECT * FROM ai_call WHERE status='open' ORDER BY id LIMIT ?", (limit,)))
    if not rows:
        return {"scored": 0, "still_open": 0, "voided": 0}

    # 需要价格的代码
    codes = [r["symbol"] for r in rows if r["symbol"]]
    px = _closes(conn, codes)

    # 需要算均值的日期（各单元的 d1 与 d1+H）
    need_dates: set = set()
    plan = []
    for r in rows:
        d = str(r["review_date"])[:10]
        i = idx.get(d)
        if i is None:
            plan.append((r, None)); continue
        if i + 1 >= len(cal):
            plan.append((r, None)); continue
        d0 = cal[i]
        d1 = cal[i + 1]
        hs = {}
        for H in HORIZONS:
            if i + 1 + H < len(cal):
                hs[H] = cal[i + 1 + H]
                need_dates.add(hs[H])
        need_dates.add(d1)
        plan.append((r, (d0, d1, hs)))

    stats = _day_stats(conn, sorted(need_dates))
    mkt = {d: s["avg"] for d, s in stats.items()}
    brd = dict(stats)   # 广度直接复用同一次扫描，不再单独查快照表

    scored = voided = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r, p in plan:
        if p is None:
            conn.execute("UPDATE ai_call SET status='void',void_reason=? WHERE id=?",
                         ("复盘日不在交易日历内或已是最新日", r["id"]))
            voided += 1
            continue
        d0, d1, hs = p
        if not hs:
            continue                      # 连 T+1 都没到，保持 open

        sym = r["symbol"] or ""
        kind = r["kind"]
        base_close = None
        if sym:
            base_close = (px.get(sym) or {}).get(d1)
            if not base_close:
                conn.execute("UPDATE ai_call SET status='void',void_reason=? WHERE id=?",
                             ("基准日无该股行情（停牌/退市/代码不符）", r["id"]))
                voided += 1
                continue

        upd: dict = {"base_date": d1, "base_close": base_close, "scored_at": now}
        any_hit = False

        # —— 次日口径（n1）：复盘日收盘 → 次一交易日收盘。只对个股类有意义。
        # 这是进哥原话「你今天推的明天涨没涨」能直接回答的那一档。
        if sym:
            c0 = (px.get(sym) or {}).get(d0)
            c1 = (px.get(sym) or {}).get(d1)
            m1 = mkt.get(d1)
            if c0 and c1 and c0 > 0 and m1 is not None:
                n_ret = (c1 / c0 - 1.0) * 100.0
                n_ex = n_ret - m1
                upd["n1_date"] = d1
                upd["n1_ret"] = round(n_ret, 4)
                upd["n1_ex"] = round(n_ex, 4)
                upd["n1_hit"] = int(_hit(kind, r["stance"], n_ex, m1, None) or 0)
                any_hit = True

        for H in HORIZONS:
            de = hs.get(H)
            if not de:
                continue
            m_avg = mkt.get(de)
            if kind == "market":
                if m_avg is None:
                    continue
                hit = _hit(kind, r["stance"], 0.0, m_avg, brd.get(de))
                if hit is None:
                    continue
                upd["h%d_date" % H] = de
                upd["h%d_ret" % H] = round(m_avg, 4)     # 市场类：ret 记全市场均值
                upd["h%d_ex" % H] = round(m_avg, 4)
                upd["h%d_hit" % H] = int(hit)
                any_hit = True
                continue
            if kind == "mainline":
                # 弱代理：次日该板块是否仍在资金净流入前列（前 10）
                try:
                    rank = conn.execute(
                        "SELECT COUNT(*) FROM sector_chain WHERE trade_date=? AND net_amt>="
                        "(SELECT net_amt FROM sector_chain WHERE trade_date=? AND sector=?)",
                        (de, de, r["name"])).fetchone()
                    exists = conn.execute(
                        "SELECT COUNT(*) FROM sector_chain WHERE trade_date=? AND sector=?",
                        (de, r["name"])).fetchone()[0]
                except Exception:  # noqa: BLE001
                    continue
                if not exists:
                    continue
                upd["h%d_date" % H] = de
                upd["h%d_hit" % H] = int(bool(rank and rank[0] <= 10))
                any_hit = True
                continue
            # 个股类
            c2 = (px.get(sym) or {}).get(de)
            # 注意不能写 `not m_avg`：全市场等权涨幅恰好为 0.0 时它是 falsy，
            # 会把一天里所有个股的计分静默跳过 —— 这种"看起来没数据"的假象最难查。
            if not c2 or m_avg is None:
                continue
            ret = (c2 / base_close - 1.0) * 100.0
            ex = ret - m_avg
            hit = _hit(kind, r["stance"], ex, m_avg, None)
            if hit is None:
                continue
            upd["h%d_date" % H] = de
            upd["h%d_ret" % H] = round(ret, 4)
            upd["h%d_ex" % H] = round(ex, 4)
            upd["h%d_hit" % H] = int(hit)
            any_hit = True

        if any_hit:
            upd["status"] = "scored"
            scored += 1
        sets = ",".join("%s=?" % k for k in upd)
        conn.execute("UPDATE ai_call SET %s WHERE id=?" % sets, list(upd.values()) + [r["id"]])

    conn.commit()
    left = conn.execute("SELECT COUNT(*) FROM ai_call WHERE status='open'").fetchone()[0]
    return {"scored": scored, "still_open": left, "voided": voided}


# ---------------------------------------------------------------- 成绩单


_LABELS = {
    "pick": "荐股（明确推荐）",
    "opp": "个股机会（老口径）",
    "avoid": "个股减/清（看空）",
    "market": "市场方向",
    "mainline": "主线板块（弱代理）",
}

# 每档统一成一个结构，前端只渲染不计算 —— 让"数字怎么来的"留在这一个文件里。
_H_COLS = (("n1", "次日"), ("h1", "T+1"), ("h3", "T+3"), ("h5", "T+5"), ("h10", "T+10"))


def _hz_row(conn, kind: str, days: int) -> dict:
    """某一类判断、每一档的表现：命中率 / 上涨率 / 平均收益 / 平均超额 / 样本数。

    口径说明（写死在这里，前端不再解释）：
      · hit  = 命中率：看多 = 超额 > 0；看空 = 超额 < 0；市场类见 _hit
      · up   = 上涨率：**绝对收益 > 0** 的比例（进哥说的"涨跌率"）
      · ret  = 平均绝对收益（%）
      · ex   = 平均超额（%，相对同日全市场等权）
    「命中率」和「上涨率」必须分开：大盘普跌时，看多但超额为正的票照样是"看对了"，
    可账户是绿的。只报一个数就一定会误导人。
    """
    out = {"kind": kind, "label": _LABELS.get(kind, kind), "n": 0, "horizons": {}}
    for key, lab in _H_COLS:
        if key == "n1":
            sql = ("SELECT COUNT(*), SUM(n1_hit), AVG(n1_ret), AVG(n1_ex), "
                   "SUM(n1_ret > 0), SUM(n1_hit IS NOT NULL) FROM ai_call "
                   "WHERE kind=? AND status='scored' AND proxy=0 AND n1_hit IS NOT NULL "
                   "AND review_date>=date('now','-%d day')" % int(days))
        else:
            h = key
            sql = ("SELECT COUNT(*), SUM(%s_hit), AVG(%s_ret), AVG(%s_ex), "
                   "SUM(%s_ret > 0), SUM(%s_hit IS NOT NULL) FROM ai_call "
                   "WHERE kind=? AND status='scored' AND proxy=0 AND %s_hit IS NOT NULL "
                   "AND review_date>=date('now','-%d day')" % (h, h, h, h, h, h, int(days)))
        try:
            n, hit, ret, ex, up, cnt = conn.execute(sql, (kind,)).fetchone()
        except Exception:  # noqa: BLE001
            continue
        if not cnt:
            continue
        out["horizons"][key] = {
            "label": lab, "n": int(cnt),
            "hit": round(100.0 * (hit or 0) / cnt, 1),
            "up": round(100.0 * (up or 0) / cnt, 1),
            "ret": round(ret or 0.0, 2),
            "ex": round(ex or 0.0, 2),
        }
        out["n"] = max(out["n"], int(cnt))
    return out


def report(conn, days: int = 120) -> dict:
    """按 kind/时间窗汇总。只统计 status='scored' 且 proxy=0 的单元。"""
    ensure_schema(conn)
    out = {"as_of": time.strftime("%Y-%m-%d"), "groups": [], "recent": [],
           "horizons": [lab for _, lab in _H_COLS], "overall": None}
    kinds = ("pick", "opp", "avoid", "market", "mainline")
    for k in kinds:
        g = _hz_row(conn, k, days)
        if g["horizons"]:
            out["groups"].append(g)

    # 「总体」= 荐股 + 老口径机会合并（都是看多建议），这才是进哥问的"你的准确率"。
    # 不含 mainline（弱代理）与 market（不是个股）。
    merged: dict = {}
    for k in ("pick", "opp"):
        g = next((x for x in out["groups"] if x["kind"] == k), None)
        if not g:
            continue
        for key, v in g["horizons"].items():
            m = merged.setdefault(key, {"label": v["label"], "n": 0, "hit_w": 0.0,
                                        "up_w": 0.0, "ret_w": 0.0, "ex_w": 0.0})
            m["n"] += v["n"]
            m["hit_w"] += v["hit"] * v["n"]
            m["up_w"] += v["up"] * v["n"]
            m["ret_w"] += v["ret"] * v["n"]
            m["ex_w"] += v["ex"] * v["n"]
    if merged:
        ov = {"kind": "all", "label": "合计（所有看多建议）", "horizons": {}}
        for key, m in merged.items():
            n = m["n"] or 1
            ov["horizons"][key] = {
                "label": m["label"], "n": m["n"],
                "hit": round(m["hit_w"] / n, 1), "up": round(m["up_w"] / n, 1),
                "ret": round(m["ret_w"] / n, 2), "ex": round(m["ex_w"] / n, 2),
            }
        ov["n"] = max((h["n"] for h in ov["horizons"].values()), default=0)
        out["overall"] = ov

    for r in conn.execute(
            "SELECT review_date,kind,symbol,name,stance,score,n1_ret,n1_ex,n1_hit,"
            "h5_ex,h5_hit,h10_ex,h10_hit FROM ai_call "
            "WHERE status='scored' AND proxy=0 AND kind IN ('pick','opp','avoid') "
            "ORDER BY review_date DESC, id DESC LIMIT 40"):
        out["recent"].append({
            "date": r[0], "kind": r[1], "symbol": r[2], "name": r[3], "stance": r[4],
            "score": r[5] or 0,
            "n1_ret": r[6], "n1_ex": r[7], "n1_hit": r[8],
            "ex5": r[9], "hit5": r[10], "ex10": r[11], "hit10": r[12],
        })

    # 样本量提示：低于 30 条时明确告知"还只是趋势"
    tot = sum(g["n"] for g in out["groups"] if g["kind"] in ("pick", "opp", "avoid"))
    out["min_sample_ok"] = tot >= 30
    out["n_scored"] = tot
    return out


def scoreboard_text(conn, days: int = 120) -> str:
    """给大模型读的「你的历史成绩单」。

    刻意的措辞：这不是让你自评，是系统算的。要让它看见自己真实的偏差，
    而不是给它一个可以自我美化的机会。

    2026-11-02 改：加入「次日」档。原来最短的验证周期是 T+5，模型每天都能
    写一份"自己的判断还在验证中"，于是任何错误都要等一周才可能被点出来 ——
    这不是样本小，这是**反馈太慢，慢到模型可以一直自我感觉良好**。
    """
    r = report(conn, days)
    if not r["groups"]:
        return ("【你的历史成绩单】暂无已回填的计分样本（复盘后下一个交易日就会产生"
                "「次日」成绩，T+5/T+10 需要等待）。本轮不要声称自己过去判断准确，"
                "也不要在 vs_last 里自评命中率。")
    L = ["【你的历史成绩单】（由系统按可执行口径回测，**不是你自己回忆的**）",
         "口径：命中=看对方向（看多要求超额>0）；上涨率=绝对收益>0 的比例；"
         "次日=复盘日收盘→次一交易日收盘；T+N=次一交易日收盘建仓后第N个交易日收盘。"]
    for g in r["groups"]:
        if g["kind"] == "mainline":
            continue
        parts = []
        for key in ("n1", "h3", "h5", "h10"):
            h = g["horizons"].get(key)
            if not h:
                continue
            parts.append("%s n=%d 命中%.0f%% 上涨%.0f%% 均收%+.2f%% 超额%+.2f%%"
                         % (h["label"], h["n"], h["hit"], h["up"], h["ret"], h["ex"]))
        if parts:
            L.append("· %s：%s" % (g["label"], "；".join(parts)))
    ov = r.get("overall")
    if ov:
        for key in ("n1", "h5"):
            h = ov["horizons"].get(key)
            if h:
                L.append("【合计·%s】样本 %d，命中率 %.0f%%，上涨率 %.0f%%，均收 %+.2f%%，超额 %+.2f%%"
                         % (h["label"], h["n"], h["hit"], h["up"], h["ret"], h["ex"]))
    if not r["min_sample_ok"]:
        L.append("⚠️ 合计有效样本仅 %d 条（<30），以上命中率**还只是趋势，不是结论**。"
                 "在 vs_last 里必须如实说明样本不足。" % r["n_scored"])
    if r["recent"]:
        L.append("最近几条的实际结果（重点看「次日」这一列 —— 这是你昨天说的对不对）：")
        for x in r["recent"][:8]:
            n1 = x.get("n1_ret")
            L.append("  - %s %s%s（推荐分%d）：次日 %s，%s"
                     % (x["date"], (x["name"] or "") + " " if x["name"] else "",
                        x["symbol"] or "", x.get("score") or 0,
                        ("%+.2f%%" % n1) if n1 is not None else "未到",
                        "对" if x.get("n1_hit") else ("未到" if x.get("n1_hit") is None else "错")))
    return "\n".join(L)


# ---------------------------------------------------------------- 荐股对账（前端 / prompt 共用）


def picks_board(conn, date: str = "", provider: str = "") -> dict:
    """某一份复盘里的荐股清单 + 它现在的实际表现。

    前端「今日荐股」表与「上次推荐现在怎么样」两张表都吃这一个函数，
    保证页面上看到的数字和数据库里存的是同一份，不各算一遍。
    """
    ensure_schema(conn)
    sql = "SELECT * FROM ai_review WHERE status='ok' AND output_json IS NOT NULL"
    args: list = []
    if date:
        sql += " AND review_date=?"
        args.append(date)
    if provider:
        sql += " AND provider=?"
        args.append(provider)
    sql += " ORDER BY review_date DESC, id DESC LIMIT 1"
    row = None
    try:
        row = conn.execute(sql, tuple(args)).fetchone()
    except Exception:  # noqa: BLE001
        return {"ok": False, "items": []}
    if not row:
        return {"ok": True, "review_date": "", "items": [], "note": "这一天还没有复盘"}

    rid, rdate = row["id"], row["review_date"]
    try:
        out = json.loads(row["output_json"] or "{}")
    except Exception:  # noqa: BLE001
        out = {}

    # **列表以模型输出为准，表现以库里的回填为准。**
    # 为什么不以库里为准：价位不全的推荐不会入账（见 record_from_review），
    # 若列表从库里取，那只"凑数的占位票"就会从页面上消失 —— 而他需要看见
    # 模型凑过数。反过来，表现只能从库里取（那是系统算的，不能由模型给）。
    perf: dict = {}
    try:
        for r in conn.execute(
                "SELECT symbol,n1_date,n1_ret,n1_ex,n1_hit,h1_ret,h1_hit,h3_ret,h3_hit,"
                "h5_ret,h5_hit,h10_ret,h10_hit,status,void_reason "
                "FROM ai_call WHERE review_id=? AND kind='pick'", (rid,)):
            perf[r[0] or ""] = {
                "n1": {"date": r[1], "ret": r[2], "ex": r[3], "hit": r[4]},
                "h1": {"ret": r[5], "hit": r[6]},
                "h3": {"ret": r[7], "hit": r[8]},
                "h5": {"ret": r[9], "hit": r[10]},
                "h10": {"ret": r[11], "hit": r[12]},
                "status": r[13],
                "void_reason": r[14],
            }
    except Exception:  # noqa: BLE001
        log.exception("[review_score] picks_board 读回填失败")

    items = []
    for it in (out.get("picks") or []):
        if not isinstance(it, dict):
            continue
        sym = _symbol_of(it, None)
        p = perf.get(sym) or {}
        items.append({
            "symbol": sym, "name": it.get("name") or "",
            "score": it.get("score") or 0,
            "score_breakdown": it.get("score_breakdown") or {},
            "action": it.get("action") or "",
            "entry_low": it.get("entry_low"), "entry_high": it.get("entry_high"),
            "stop": it.get("stop"), "target": it.get("target"),
            "horizon": it.get("horizon"),
            "thesis": it.get("thesis") or "",
            "why_now": it.get("why_now") or "",
            "evidence": it.get("evidence") or [],
            "invalidate": it.get("invalidate") or "",
            "n1": p.get("n1"), "h1": p.get("h1"), "h3": p.get("h3"),
            "h5": p.get("h5"), "h10": p.get("h10"),
            # 入账过 = 可验证；没入账说明这条被入口校验挡了（多半是价位不全）
            "tracked": bool(p),
            "status": p.get("status") or "untracked",
            "void_reason": p.get("void_reason") or "",
        })

    # 老数据（picks 段出现之前）只有 ai_call 里的记录，用它们兜底
    if not items:
        for sym, p in perf.items():
            items.append({"symbol": sym, "name": "", "score": 0, "action": "",
                          "entry_low": None, "entry_high": None, "stop": None,
                          "target": None, "horizon": None, "thesis": "", "why_now": "",
                          "evidence": [], "invalidate": "",
                          "n1": p.get("n1"), "h1": p.get("h1"), "h3": p.get("h3"),
                          "h5": p.get("h5"), "h10": p.get("h10"),
                          "tracked": True, "status": p.get("status") or "open",
                          "void_reason": p.get("void_reason") or ""})
        # 名称从 ai_call 补
        try:
            for r in conn.execute(
                    "SELECT symbol,name,thesis FROM ai_call WHERE review_id=? AND kind='pick'", (rid,)):
                for x in items:
                    if x["symbol"] == (r[0] or ""):
                        x["name"], x["thesis"] = r[1] or "", r[2] or ""
        except Exception:  # noqa: BLE001
            pass
    items.sort(key=lambda x: -(x.get("score") or 0))

    return {
        "ok": True,
        "review_id": rid,
        "review_date": rdate,
        "provider": row["provider"],
        "verdict": out.get("verdict") or "",
        "items": items,
        "n": len(items),
    }


def picks_context(conn, date: str, limit: int = 8) -> str:
    """给模型读的「你上次推的票，现在什么结果」—— 逐条摊开，逼它面对而不是回忆。

    与 vs_last 的区别：vs_last 是模型自己写的自评。这里给的是**系统算出的价格事实**，
    模型只能在事实之上做归因（"我错在哪"），不能改写成"我早就说过"。
    """
    ensure_schema(conn)
    try:
        row = conn.execute(
            "SELECT id, review_date FROM ai_review WHERE status='ok' AND review_date<? "
            "AND output_json IS NOT NULL ORDER BY review_date DESC, id DESC LIMIT 1",
            (date,)).fetchone()
    except Exception:  # noqa: BLE001
        return ""
    if not row:
        return "【上次荐股的结果】这是第一次留痕，没有可对照的历史。"
    rid, rdate = row["id"], row["review_date"]
    rows = []
    try:
        rows = list(conn.execute(
            "SELECT symbol,name,score,action,n1_ret,n1_ex,n1_hit,h5_ret,h5_ex,h5_hit,"
            "status,void_reason FROM ai_call WHERE review_id=? AND kind='pick' "
            "ORDER BY score DESC, id LIMIT ?", (rid, limit)))
    except Exception:  # noqa: BLE001
        return ""
    if not rows:
        return "【上次荐股的结果】%s 那份复盘没有给出带代码的个股推荐，无可对账。" % rdate

    L = ["【上次荐股的结果 —— 系统按收盘价回填的事实，不是你的回忆】",
         "复盘日 %s，你推了 %d 只（按推荐分排序）：" % (rdate, len(rows))]
    def _num(v, default=None):
        """ai_call 的收益列在库里可能是 TEXT（历史回填/导入留下），
        直接拿去 %+.2f 会 TypeError 把整个对账单上下文打没（2026-11-02 实测：
        server-20261102.log「上次荐股对账单生成失败」）。统一先转 float。"""
        if v is None or v == "":
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    for r in rows:
        sym, nm, sc = r[0], r[1], _num(r[2], 0)
        n1r, n1e, n1h = _num(r[3]), _num(r[4]), r[5]
        h5r, h5e, h5h = _num(r[6]), _num(r[7]), r[8]
        st, vr = r[9], r[10]
        seg = "· %s %s（推荐分 %d）" % (sym or "-", nm or "", sc)
        if st == "void":
            seg += "：%s —— 无法验证，不计入成绩。" % (vr or "数据缺失")
        elif n1r is None:
            seg += "：结果未到（次日收盘价还没产生）。"
        else:
            seg += "：次日 %+.2f%%（超额 %+.2f%%，%s）" % (n1r, n1e or 0, "对" if n1h else "错")
            if h5r is not None:
                seg += "；T+5 %+.2f%%（超额 %+.2f%%，%s）" % (h5r, h5e or 0, "对" if h5h else "错")
        L.append(seg)
    L.append("要求：在 picks_review 段里逐条回答——① 这一条当时的核心依据今天还成立吗；"
             "② 如果不成立，是哪一条依据先失效的（要指到具体指标/数据）；"
             "③ 下一次遇到同样结构，你要改什么。**不许用「市场环境变化」这类空话带过**，"
             "必须指到可核对的东西。")
    return "\n".join(L)


if __name__ == "__main__":
    # 自检：python viewer/review_score.py
    import os
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    print(scoreboard_text(con))
    print("\n" + "=" * 70)
    print(json.dumps(report(con), ensure_ascii=False, indent=1)[:1500])
