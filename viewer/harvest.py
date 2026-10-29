# -*- coding: utf-8 -*-
"""收割视角引擎（harvest）—— 「这一局在哪里、什么动作必亏」。

与 regime.py 的分工
--------------------
regime.py  ：市场处于牛/熊/震荡（回答「择时环境」，用于给信号战绩分层）
harvest.py ：收割周期（回答「这一局里谁会挨打」，用于避坑与反向操作）

设计依据（全部实测，非推断，数据见 docs/大势研判_20261031.md）
-----------------------------------------------------------------
1. **「连板开板 = 派发」是库内最强信号**（2023-01 ~ 2026-10，n=896）：
   昨日已封板 ≥3 板、今日触及涨停价但收盘未封（散户终于买得进）
   → T+5 超额 **−6.13%**，t=**−10.68**，胜率 28.3%。
   同期「≥3 板且当日仍封板（买不进）」T+5 超额 +1.92%(t=+3.56)。
   同一个强势股池，只差「你买不买得进」，差 8 个百分点。
   分板数：2 板 −3.90%(t=−9.55) / 3 板 −5.68% / 4-5 板 **−7.21%**。

2. **市场层「情绪指标」预测不了指数方向**（2019-2026，1,600+ 交易日）：
   涨停少 −0.09%(t=0.63)、炸板率高 +0.16%(t=1.31)、连板断档 −0.13%(t=0.72) 均不显著。
   唯一显著是均值回复：跌停潮(>P90) 后 T+5 **+0.93%**(t=2.87)、T+10 +1.01%(t=3.00)。
   → 本模块**不做指数点位预测**，只做「状态识别 + 必亏动作清单」。

口径（沿用 DECISIONS D8/D14）
------------------------------
- 涨停价：主板 9.75%、双创 19.5%、北交所 29.5%（留 0.25% 缓冲避浮点误差）；
- ST 未单独处理：ST 涨停 5% 不会触及 10% 线，故不会被误判成「开板陷阱」，
  只影响涨停家数计数（实测误差 <2%）；
- 超额 = 个股收益 − 同日全市场等权收益；t 值按日聚合后检验。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta

KLINE_DB = os.path.join("data", "kline_daily.db")

LIM_MAIN = 0.0975      # 主板 10%
LIM_GEM = 0.195        # 创业板 300/301、科创板 688/689 → 20%
LIM_BJ = 0.295         # 北交所 43/83/87/88/92 → 30%

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_structure_daily (
    trade_date   TEXT PRIMARY KEY,
    pool_n       INTEGER,
    zt_n         INTEGER,   -- 收盘涨停家数
    zb_n         INTEGER,   -- 炸板家数（触及涨停价但收盘未封）
    dt_n         INTEGER,   -- 收盘跌停家数
    zb_rate      REAL,      -- 炸板率 = zb/(zt+zb)
    max_streak   INTEGER,   -- 最高连板高度
    lb_n         INTEGER,   -- 连板（>=2）家数
    median_pct   REAL,      -- 中位涨跌幅 %
    up_ratio     REAL,      -- 上涨家数占比
    eq_nav       REAL,      -- 全市场等权净值（首日=1）
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_msd_date ON market_structure_daily(trade_date);

CREATE TABLE IF NOT EXISTS harvest_trap_daily (
    trade_date   TEXT,
    code         TEXT,
    name         TEXT,
    prev_streak  INTEGER,   -- 昨日已封板数
    close_pct    REAL,      -- 当日涨幅 %
    turned       INTEGER,   -- 收盘是否翻绿
    PRIMARY KEY (trade_date, code)
);
CREATE INDEX IF NOT EXISTS idx_htd_date ON harvest_trap_daily(trade_date);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _open_kline():
    if not os.path.exists(KLINE_DB):
        return None
    con = sqlite3.connect("file:%s?mode=ro" % KLINE_DB, uri=True)
    con.row_factory = sqlite3.Row
    return con


# 基础 CTE：一次算好 pc / up / touch / streak
_BASE_CTE = """
WITH t AS (
  SELECT code, d, c, h, LAG(c) OVER (PARTITION BY code ORDER BY d) AS pc
  FROM kline WHERE d >= :since
), u AS (
  SELECT code, d, c, h, pc,
    CASE WHEN code LIKE '30%' OR code LIKE '68%' THEN 0.195
         WHEN code LIKE '43%' OR code LIKE '83%' OR code LIKE '87%'
           OR code LIKE '88%' OR code LIKE '92%' THEN 0.295
         ELSE 0.0975 END AS lim
  FROM t WHERE pc > 0
), v AS (
  SELECT code, d, c, h, pc, lim,
    CASE WHEN c >= pc*(1+lim) THEN 1 ELSE 0 END AS up,
    CASE WHEN h >= pc*(1+lim) THEN 1 ELSE 0 END AS touch
  FROM u
), g AS (
  SELECT code, d, c, pc, up, touch, lim,
    SUM(CASE WHEN up=0 THEN 1 ELSE 0 END) OVER (PARTITION BY code ORDER BY d) AS grp
  FROM v
), s AS (
  SELECT code, d, c, pc, up, touch, lim,
    SUM(up) OVER (PARTITION BY code, grp ORDER BY d) AS streak
  FROM g
)
"""


# --------------------------------------------------------------------------- #
# 结构指标：回填 / 增量
# --------------------------------------------------------------------------- #
def refresh_daily(conn: sqlite3.Connection, since: int = 20200101,
                  keep_days: int = 400) -> dict:
    """刷新结构指标与陷阱名单。首次全量回填，之后只重算最近 keep_days。"""
    ensure_schema(conn)
    kr = _open_kline()
    if kr is None:
        return {"ok": False, "error": "日线库缺失：%s" % KLINE_DB}
    try:
        done = conn.execute(
            "SELECT MAX(trade_date) FROM market_structure_daily").fetchone()[0]
        if done:
            start = int((datetime.strptime(str(done), "%Y%m%d")
                         - timedelta(days=int(keep_days * 1.6))).strftime("%Y%m%d"))
        else:
            start = since

        rows = kr.execute(_BASE_CTE + """
        SELECT d AS trade_date, COUNT(*) AS pool_n,
               SUM(up) AS zt_n,
               SUM(CASE WHEN touch=1 AND up=0 THEN 1 ELSE 0 END) AS zb_n,
               SUM(CASE WHEN c <= pc*(1-lim) THEN 1 ELSE 0 END) AS dt_n,
               MAX(streak) AS max_streak,
               SUM(CASE WHEN streak>=2 THEN 1 ELSE 0 END) AS lb_n
        FROM s GROUP BY d ORDER BY d
        """, {"since": start}).fetchall()
        if not rows:
            return {"ok": True, "inserted": 0, "note": "无新数据"}

        # 中位涨跌幅 + 上涨占比（逐日取样算中位数，SQLite 无 median）
        raw = kr.execute(_BASE_CTE + """
        SELECT d, c/pc-1 AS r, CASE WHEN c>pc THEN 1 ELSE 0 END AS upf
        FROM s WHERE d >= :lo
        """, {"since": start, "lo": min(r["trade_date"] for r in rows)}).fetchall()
        buf: dict = {}
        for r in raw:
            buf.setdefault(r["d"], []).append((r["r"], r["upf"]))
        stat = {}
        for d, vals in buf.items():
            rs = sorted(v[0] for v in vals)
            n = len(rs)
            if n:
                stat[d] = (rs[n // 2] * 100,
                           sum(v[1] for v in vals) / n)

        dates = [r["trade_date"] for r in rows]
        base = conn.execute(
            "SELECT eq_nav FROM market_structure_daily "
            "WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1",
            (str(dates[0]),)).fetchone()
        prev_nav = float(base[0]) if (base and base[0]) else 1.0

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ins = 0
        for r in rows:
            d = r["trade_date"]
            mpct, upr = stat.get(d, (0.0, 0.0))
            nav = prev_nav * (1 + (mpct or 0) / 100.0)
            zt, zb = r["zt_n"] or 0, r["zb_n"] or 0
            conn.execute(
                "INSERT OR REPLACE INTO market_structure_daily"
                "(trade_date,pool_n,zt_n,zb_n,dt_n,zb_rate,max_streak,lb_n,"
                " median_pct,up_ratio,eq_nav,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(d), r["pool_n"], zt, zb, r["dt_n"] or 0,
                 (zb / (zt + zb)) if (zt + zb) else 0.0,
                 r["max_streak"] or 0, r["lb_n"] or 0,
                 round(mpct or 0.0, 4), round(upr or 0.0, 4),
                 round(nav, 6), now))
            prev_nav = nav
            ins += 1
        conn.commit()

        trap_n = _refresh_traps(conn, kr, str(dates[0]))
        return {"ok": True, "inserted": ins, "traps": trap_n,
                "range": [str(dates[0]), str(dates[-1])]}
    finally:
        kr.close()


def _refresh_traps(conn: sqlite3.Connection, kr, since: str) -> int:
    """重算陷阱名单：昨日连板 ≥2 且今日开板。

    首板开板（prev_streak=1）同样有效（T+5 −3.83%，t=−18.51），但强度弱于 ≥3 板，
    故入库门槛设 ≥2，前端按板数分档展示红/橙/黄。
    """
    rows = kr.execute(_BASE_CTE + """
    SELECT d, code, c, pc, up, touch, streak,
           LAG(streak) OVER (PARTITION BY code ORDER BY d) AS prev_streak
    FROM s WHERE d >= :since2
    """, {"since": int(since), "since2": int(since)}).fetchall()
    n = 0
    for r in rows:
        ps = r["prev_streak"] or 0
        if ps >= 2 and r["touch"] and not r["up"]:
            conn.execute(
                "INSERT OR REPLACE INTO harvest_trap_daily"
                "(trade_date,code,name,prev_streak,close_pct,turned)"
                " VALUES(?,?,?,?,?,?)",
                (str(r["d"]), r["code"], None, ps,
                 round((r["c"] / r["pc"] - 1) * 100, 2),
                 1 if r["c"] < r["pc"] else 0))
            n += 1
    conn.commit()
    return n


# --------------------------------------------------------------------------- #
# 状态识别
# --------------------------------------------------------------------------- #
def _pctile(vals: list, v: float) -> float:
    if not vals:
        return 0.5
    s = sorted(vals)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi) // 2
        if s[mid] < v:
            lo = mid + 1
        else:
            hi = mid
    return lo / len(s)


def _phase(cur: dict, hist: list) -> dict:
    """只使用实测中已证明有方向性的量，不掺未验证指标。"""
    zt, dt, zb = cur["zt_n"], cur["dt_n"], cur["zb_rate"] or 0.0
    ms, lb = cur["max_streak"] or 0, cur["lb_n"] or 0
    zt_q = _pctile([h["zt_n"] for h in hist], zt)
    dt_q = _pctile([h["dt_n"] for h in hist], dt)
    zb_q = _pctile([h["zb_rate"] or 0 for h in hist], zb)

    if dt_q >= 0.90 and zt_q <= 0.40:
        tag, lvl = "杀跌", 3
        desc = ("跌停 %d 家（历史 %d%% 分位）、涨停仅 %d 家（%d%% 分位）—— 亏钱效应扩散。"
                "实测此状态后 T+5 市场等权 +0.93%%(t=2.87)，恐慌后多有反弹，"
                "但**反弹不等于反转**。" % (dt, dt_q * 100, zt, zt_q * 100))
    elif zb_q >= 0.85 and zt_q <= 0.40:
        tag, lvl = "派发", 3
        desc = ("炸板率 %.0f%%（%d%% 分位）而涨停仅 %d 家 —— 封板接不住，"
                "资金在借强势出货。" % (zb * 100, zb_q * 100, zt))
    elif ms >= 8 and lb >= 20:
        tag, lvl = "过热", 2
        desc = ("最高 %d 连板、连板股 %d 家 —— 接力链条拉满，此时进场买的是最后一棒。"
                % (ms, lb))
    elif zt_q >= 0.70 and zb_q <= 0.40:
        tag, lvl = "健康", 1
        desc = ("涨停 %d 家（%d%% 分位）、炸板率 %.0f%%（%d%% 分位）—— 封板质量好，"
                "赚钱效应在扩散。" % (zt, zt_q * 100, zb * 100, zb_q * 100))
    else:
        tag, lvl = "拉锯", 2
        desc = ("涨停 %d 家、炸板率 %.0f%%、最高 %d 连板 —— 无明确方向，"
                "既没赚钱效应扩散，也没到恐慌极值。" % (zt, zb * 100, ms))
    return {"tag": tag, "level": lvl, "desc": desc,
            "pct": {"zt": round(zt_q, 3), "dt": round(dt_q, 3), "zb": round(zb_q, 3)}}


def _position(hist_asc: list) -> dict:
    if len(hist_asc) < 20:
        return {"ma20_dev": None, "ma60_dev": None, "note": "样本不足"}
    nav = [h["eq_nav"] for h in hist_asc]
    cur = nav[-1]
    ma20 = sum(nav[-20:]) / 20
    ma60 = sum(nav[-60:]) / min(60, len(nav))
    d20 = (cur / ma20 - 1) * 100
    d60 = (cur / ma60 - 1) * 100
    if d20 > 5:
        note = "等权指数高于 20 日线 %.1f%%，位置偏高，追高性价比差" % d20
    elif d20 < -5:
        note = "等权指数低于 20 日线 %.1f%%，位置偏低" % d20
    else:
        note = "等权指数贴住 20 日线（偏离 %+.1f%%），区间震荡" % d20
    return {"ma20_dev": round(d20, 2), "ma60_dev": round(d60, 2), "note": note}


# --------------------------------------------------------------------------- #
# 对外
# --------------------------------------------------------------------------- #
def snapshot(conn: sqlite3.Connection, hist_days: int = 250):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM market_structure_daily ORDER BY trade_date DESC LIMIT ?",
        (hist_days,)).fetchall()
    if not rows:
        return None
    hist = [dict(r) for r in rows]
    cur, older = hist[0], hist[1:]
    return {
        "date": cur["trade_date"],
        "raw": {k: cur[k] for k in (
            "pool_n", "zt_n", "zb_n", "dt_n", "zb_rate", "max_streak",
            "lb_n", "median_pct", "up_ratio")},
        "phase": _phase(cur, older),
        "position": _position(hist[::-1]),
        "hist_days": len(hist),
    }


def traps(conn: sqlite3.Connection, date: str | None = None, limit: int = 30) -> dict:
    conn.row_factory = sqlite3.Row
    d = date or (conn.execute(
        "SELECT MAX(trade_date) FROM harvest_trap_daily").fetchone() or [None])[0]
    if not d:
        return {"date": None, "rows": [], "total": 0}
    rows = conn.execute(
        "SELECT t.code, t.prev_streak, t.close_pct, t.turned, "
        "COALESCE(sn.name,'') AS nm "
        "FROM harvest_trap_daily t LEFT JOIN stock_names sn ON sn.code = t.code "
        "WHERE t.trade_date = ? ORDER BY t.prev_streak DESC, t.close_pct ASC LIMIT ?",
        (d, limit)).fetchall()
    out = [{
        "code": r["code"], "name": r["nm"] or r["code"],
        "prev_streak": r["prev_streak"], "close_pct": r["close_pct"],
        "turned": bool(r["turned"]),
        "risk": "极高" if r["prev_streak"] >= 4 else ("高" if r["prev_streak"] >= 3 else "中"),
    } for r in rows]
    total = conn.execute(
        "SELECT COUNT(*) FROM harvest_trap_daily WHERE trade_date = ?", (d,)).fetchone()[0]
    return {"date": d, "rows": out, "total": total}


def recent_trap_stats(conn: sqlite3.Connection, days: int = 8) -> dict:
    rows = list(reversed(conn.execute(
        "SELECT trade_date, COUNT(*) n, "
        "SUM(CASE WHEN prev_streak>=3 THEN 1 ELSE 0 END) n3 "
        "FROM harvest_trap_daily GROUP BY trade_date "
        "ORDER BY trade_date DESC LIMIT ?", (days,)).fetchall()))
    latest = rows[-1][1] if rows else 0
    prev = rows[-2][1] if len(rows) >= 2 else 0
    return {
        "series": [{"date": r[0], "n": r[1], "n3": r[2]} for r in rows],
        "latest": latest,
        "trend": "加速" if latest > prev else ("收敛" if latest < prev else "平稳"),
    }


def payload(conn: sqlite3.Connection) -> dict:
    snap = snapshot(conn)
    if not snap:
        return {"ok": False, "error": "结构数据未生成，请先跑 regime_daily 任务"}
    return {
        "ok": True,
        "asof": snap["date"],
        "phase": snap["phase"],
        "position": snap["position"],
        "raw": snap["raw"],
        "traps": traps(conn, snap["date"]),
        "trap_trend": recent_trap_stats(conn),
        "evidence": EVIDENCE,
    }


# 供 AI 与前端引用的实测数字（与 docs/大势研判_20261031.md 同源）
EVIDENCE = {
    "harvest": {
        "rule": "昨日连板≥3，今日触及涨停未封（开板）",
        "n": 896, "horizon": 5, "excess": -6.13, "t": -10.68, "win": 28.3,
        "control": {"rule": "≥3板且当日仍封板（买不进）",
                    "n": 2410, "excess": 1.92, "t": 3.56, "win": 44.2},
    },
    "by_streak": [
        {"streak": "2板", "n": 1290, "excess": -3.90, "t": -9.55},
        {"streak": "3板", "n": 447, "excess": -5.68, "t": -7.63},
        {"streak": "4-5板", "n": 330, "excess": -7.21, "t": -7.63},
    ],
    "market_useless": [
        {"cond": "涨停少(<P10)", "excess": -0.09, "t": 0.63},
        {"cond": "炸板率高(>P90)", "excess": 0.16, "t": 1.31},
        {"cond": "连板断档", "excess": -0.13, "t": 0.72},
    ],
    "mean_reversion": [
        {"cond": "跌停潮(>P90) 后 T+5", "excess": 0.93, "t": 2.87},
        {"cond": "跌停潮(>P90) 后 T+10", "excess": 1.01, "t": 3.00},
    ],
}


def text_block(conn: sqlite3.Connection) -> str:
    """给 AI 复盘的文本块：局面 + 陷阱 + 证据边界。"""
    p = payload(conn)
    if not p.get("ok"):
        return ""
    ph, pos, raw = p["phase"], p["position"], p["raw"]
    L = ["\n## 七、局面（结构层，全部来自日线实测）"]
    L.append("状态：**%s**（强度 %d/3）— %s" % (ph["tag"], ph["level"], ph["desc"]))
    L.append("盘面：涨停 %d ｜跌停 %d ｜炸板率 %.0f%% ｜最高 %d 连板 ｜连板股 %d 家 ｜中位涨幅 %+.2f%%"
             % (raw["zt_n"], raw["dt_n"], (raw["zb_rate"] or 0) * 100,
                raw["max_streak"], raw["lb_n"], raw["median_pct"] or 0))
    L.append("位置：%s" % pos.get("note"))
    tr = p["traps"]
    if tr.get("rows"):
        L.append("\n**今日陷阱名单（昨日连板≥2、今日开板 = 散户终于买得进的时刻）共 %d 只**："
                 % tr["total"])
        for x in tr["rows"][:12]:
            L.append("  - %s(%s) 昨 %d 板，今收 %+.2f%%%s"
                     % (x["name"], x["code"], x["prev_streak"], x["close_pct"],
                        "（翻绿）" if x["turned"] else ""))
        L.append("  历史同型 T+5 超额 −6.13%；4-5 板开板 −7.21%，板数越高越惨。")
    else:
        L.append("\n今日无「连板开板」陷阱标的。")
    tt = p["trap_trend"]
    if tt.get("series"):
        L.append("近 %d 日陷阱数：%s（%s）"
                 % (len(tt["series"]), " → ".join(str(s["n"]) for s in tt["series"]),
                    tt["trend"]))
    L.append("\n**必须遵守的边界**：市场层情绪指标（涨停家数/炸板率/连板断档）对指数方向"
             "**没有预测力**（t 值均 <1.6，已实测），不得据此断言「明天要涨/要跌」。"
             "唯一显著的是极端恐慌后的均值回复（跌停潮后 T+5 +0.93%，t=2.87），"
             "但扣成本后有限，只能作「情绪位置」参考。")
    return "\n".join(L)
