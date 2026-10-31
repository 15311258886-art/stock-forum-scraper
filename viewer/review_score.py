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

HORIZONS = (5, 10)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_call (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id     INTEGER,                 -- 关联 ai_review.id
    review_date   TEXT NOT NULL,           -- 复盘针对的交易日
    provider      TEXT NOT NULL,
    kind          TEXT NOT NULL,           -- opp / avoid / market / mainline
    symbol        TEXT,                    -- 个股代码（market/mainline 为空）
    name          TEXT,
    stance        TEXT,                    -- bull / bear / state:<普涨…>
    thesis        TEXT,
    trigger       TEXT,
    invalidate    TEXT,
    -- 回填区
    base_date     TEXT,                    -- 建仓基准日 d1（d 之后第一个交易日）
    base_close    REAL,
    h5_date       TEXT, h5_ret REAL, h5_ex REAL, h5_hit INTEGER,
    h10_date      TEXT, h10_ret REAL, h10_ex REAL, h10_hit INTEGER,
    proxy         INTEGER DEFAULT 0,        -- 1 = 弱代理，不进合并命中率
    status        TEXT NOT NULL DEFAULT 'open',   -- open / scored / void
    void_reason   TEXT,
    scored_at     TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ai_call_date ON ai_call(review_date, kind);
CREATE INDEX IF NOT EXISTS ix_ai_call_stat ON ai_call(status, id);
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

    # ① 机会（看多）
    for it in (output.get("opportunities") or [])[:8]:
        if not isinstance(it, dict):
            continue
        sym = _symbol_of(it, name2code)
        rows.append((review_id, review_date, provider, "opp", sym,
                     _clip(it.get("name"), 20), "bull",
                     _clip(it.get("thesis"), 200), _clip(it.get("trigger"), 120),
                     _clip(it.get("invalidate"), 120), 0))

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
        rows.append((review_id, review_date, provider, "avoid", sym,
                     _clip(it.get("name"), 20), stance,
                     _clip(it.get("reason"), 200), "", "", 0))

    # ③ 市场方向
    mr = output.get("market_read") or {}
    state = str(mr.get("state") or "").strip()
    if state:
        rows.append((review_id, review_date, provider, "market", "", "",
                     "state:%s" % state, _clip(mr.get("logic"), 300), "", "", 0))

    # ④ 主线板块（弱代理）
    for it in (output.get("mainlines") or [])[:6]:
        if not isinstance(it, dict) or not it.get("sector"):
            continue
        rows.append((review_id, review_date, provider, "mainline", "",
                     _clip(it.get("sector"), 24), "bull",
                     _clip(it.get("logic"), 200), _clip(it.get("stage"), 20), "", 1))

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
            "thesis,trigger,invalidate,proxy,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
        d1 = cal[i + 1]
        hs = {}
        for H in HORIZONS:
            if i + 1 + H < len(cal):
                hs[H] = cal[i + 1 + H]
                need_dates.add(hs[H])
        need_dates.add(d1)
        plan.append((r, (d1, hs)))

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
        d1, hs = p
        if not hs:
            continue                      # 还没到 T+5，保持 open

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
            if not c2 or not m_avg:
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


def report(conn, days: int = 120) -> dict:
    """按 kind/时间窗汇总命中率。只统计 status='scored' 且 proxy=0 的单元。"""
    ensure_schema(conn)
    out = {"as_of": time.strftime("%Y-%m-%d"), "groups": [], "recent": []}
    base = ("SELECT kind, COUNT(*), AVG(h5_ex), SUM(h5_hit), AVG(h10_ex), SUM(h10_hit), "
            "SUM(h5_hit IS NOT NULL), SUM(h10_hit IS NOT NULL) FROM ai_call "
            "WHERE status='scored' AND proxy=0 AND review_date>=date('now','-%d day') " % int(days))
    labels = {"opp": "个股机会（看多）", "avoid": "个股减/清（看空）",
              "market": "市场方向", "mainline": "主线板块（弱代理）"}
    for kind, n, ex5, hit5, ex10, hit10, c5, c10 in conn.execute(base + " GROUP BY kind"):
        out["groups"].append({
            "kind": kind, "label": labels.get(kind, kind), "n": n,
            "ex5": round(ex5, 3) if ex5 is not None else None,
            "hit5": round(100.0 * (hit5 or 0) / c5, 1) if c5 else None, "n5": c5 or 0,
            "ex10": round(ex10, 3) if ex10 is not None else None,
            "hit10": round(100.0 * (hit10 or 0) / c10, 1) if c10 else None, "n10": c10 or 0,
        })
    for r in conn.execute(
            "SELECT review_date,kind,symbol,name,stance,h5_ex,h5_hit,h10_ex,h10_hit "
            "FROM ai_call WHERE status='scored' AND proxy=0 ORDER BY review_date DESC,id DESC LIMIT 20"):
        out["recent"].append({
            "date": r[0], "kind": r[1], "symbol": r[2], "name": r[3], "stance": r[4],
            "ex5": r[5], "hit5": r[6], "ex10": r[7], "hit10": r[8],
        })
    # 样本量提示：低于 30 条时明确告知"还只是趋势"
    tot = sum(g["n5"] for g in out["groups"] if g["kind"] in ("opp", "avoid"))
    out["min_sample_ok"] = tot >= 30
    out["n_scored"] = tot
    return out


def scoreboard_text(conn, days: int = 120) -> str:
    """给大模型读的「你的历史成绩单」。

    刻意的措辞：这不是让你自评，是系统算的。要让它看见自己真实的偏差，
    而不是给它一个可以自我美化的机会。
    """
    r = report(conn, days)
    if not r["groups"]:
        return ("【你的历史成绩单】暂无已回填的计分样本（需要复盘后满 5 个交易日才会产生）。"
                "本轮不要声称自己过去判断准确，也不要在 vs_last 里自评命中率。")
    L = ["【你的历史成绩单】（由系统按可执行口径回测，**不是你自己回忆的**）",
         "口径：复盘日次一交易日收盘建仓，T+5/T+10 收盘计，超额=相对同日全市场等权。"]
    for g in r["groups"]:
        if g["kind"] == "mainline":
            L.append("· %s：n=%d（弱代理，不计入合并命中率）" % (g["label"], g["n"]))
            continue
        L.append("· %s：n=%d，T+5 超额 %+.2f%%，命中率 %s%%（%d 条）；T+10 超额 %s，命中率 %s%%"
                 % (g["label"], g["n"], g["ex5"] or 0,
                    ("%.0f" % g["hit5"]) if g["hit5"] is not None else "-", g["n5"],
                    ("%+.2f%%" % g["ex10"]) if g["ex10"] is not None else "-",
                    ("%.0f" % g["hit10"]) if g["hit10"] is not None else "-"))
    if not r["min_sample_ok"]:
        L.append("⚠️ 合计有效样本仅 %d 条（<30），以上命中率**还只是趋势，不是结论**。"
                 "在 vs_last 里必须如实说明样本不足。" % r["n_scored"])
    if r["recent"]:
        L.append("最近几条的实际结果：")
        for x in r["recent"][:6]:
            L.append("  - %s %s%s：T+5 超额 %s，%s"
                     % (x["date"], (x["name"] or "") + " " if x["name"] else "",
                        x["symbol"] or "", ("%+.2f%%" % x["ex5"]) if x["ex5"] is not None else "未到",
                        "对" if x["hit5"] else ("未到" if x["hit5"] is None else "错")))
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
