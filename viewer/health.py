# -*- coding: utf-8 -*-
"""调度任务运行记录 + 数据新鲜度（2026-10-31 决策⑤）。

## 要解决的问题

系统里已有的状态表是**抓取源**级的（`refresh_status`：16 个 source 的成败），
但**没有任务级的**：一个调度任务失败，只会在 `viewer/logs/sched-<job>.log`
里留下一行，而没有人会去翻日志。于是同一个剧本反复上演：

  · `build_exclusion` 自 2026-10-26 上线起 **11 次执行 11 次失败、0 次成功**，
    因为 argparse 抛的 `SystemExit` 不被 `except Exception` 捕获，日志里只留一行 start；
  · `a_shares_snapshot` 连续两天抓取失败（Remote end closed connection）
    却在日志里回报成功，父进程据此写下当日「已完成」标记 → 当天不再重试 →
    整条日更链静默停在 10-27。

两次都不是"没有日志"，是"日志没人看"。所以这里不写更多日志，
而是把每次运行**落成一行可查询的记录**，并让"连续失败"变成一个可以直接看的数字。

## 两个刻意的口径选择

**1. 滞后按交易日算，不按自然日。**
按自然日算，每个周一早上所有表都会报"陈旧 2 天"，报多了就没人看告警了。
这里用 `rhythm.is_trading_day_now()` 逐日判定，周末与节假日自动不计入。
日历不可用时返回 None 并标 `unknown` —— 不退回自然日猜测。

**2. 基准是"今天"，不是"内部最新日期"。**
这是最容易搞反的一处：如果拿 `max(kline.d)` 当基准去和其他表比，
那么整条采集链全停了的时候，所有表都"和基准一致"→ 显示新鲜。
恰恰是要抓的那种故障，会被这个口径藏起来。所以基准只能是外部时钟。
内部最新日期作为独立字段展示，供人判断"停在哪一天"。

## 只读约定

`record_job_run()` 是唯一写入口，只写 `job_runs` 一张表。
其余函数全部只读，不改任何业务数据。
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 2026-10-31 修复（隔离能力漏网项）：库路径改为可被 `VIEWER_DB` 覆写，与 viewer/db.py
#   的 `DB_PATH` 同一口径。2026-09-22「示例数据覆盖真实数据」整改时定下了这条规矩，
#   但 health.py 当时没跟进（同类硬编码全仓另有约 122 处，尚未整改）。
#   后果是：任何跑到 `record_job_run()` 的测试都会把假运行记录写进**生产** job_runs
#   （实测 tests/test_kline_daily_exitcode.py 每次跑留 3 行，累积 18 条 ok=0/err='x'，
#   一度被误判成 kline_daily 的真实故障）。未设该变量时行为不变，仍写生产库。
APP_DB = os.environ.get("VIEWER_DB") or os.path.join(ROOT, "viewer", "app.db")
KLINE_DB = os.path.join(ROOT, "data", "kline_daily.db")

_DDL = """
CREATE TABLE IF NOT EXISTS job_runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job      TEXT NOT NULL,
    started  TEXT,
    ended    TEXT,
    elapsed  REAL,
    ok       INTEGER,
    rc       INTEGER,
    error    TEXT,
    created  TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job, ended DESC);
CREATE INDEX IF NOT EXISTS idx_job_runs_ended ON job_runs(ended DESC);
"""

# 新鲜度目标：表名 → (显示名, 读哪个库, 取日期表达式, 容忍滞后交易日)
# 容忍值不是"拍脑袋"，是各源自身的发布节奏：日线/指标/快照/市况每个交易日都该有；
# 龙虎榜是收盘后发布，容 1 天；公告不是每天都有，容 3 天。
TARGETS = (
    ("kline",                  "日线行情",   "kline",  "SELECT MAX(d) FROM kline",                        0),
    ("stock_indicators_daily", "技术指标",   "app",    "SELECT MAX(date) FROM stock_indicators_daily",    0),
    ("market_regime",          "市况判定",   "app",    "SELECT MAX(trade_date) FROM market_regime",       0),
    ("stock_daily_snapshot",   "全A快照",    "app",    "SELECT MAX(trade_date) FROM stock_daily_snapshot", 0),
    ("lhb_stocks",             "龙虎榜",     "app",    "SELECT MAX(trade_date) FROM lhb_stocks",          1),
    ("stock_ann",              "公告",       "app",    "SELECT MAX(notice_date) FROM stock_ann",          3),
)


def _conn(which: str):
    p = KLINE_DB if which == "kline" else APP_DB
    if not os.path.exists(p):
        return None
    try:
        c = sqlite3.connect(p, timeout=20)
        c.execute("PRAGMA busy_timeout=20000")
        return c
    except sqlite3.Error:
        return None


def _norm_date(v):
    """把 20261031 / '20261031' / '2026-10-31' / '2026/10/31' 归一成 date，失败返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        s = str(int(v))
    else:
        s = str(v).strip()
    s = s.replace("/", "-")
    try:
        if "-" in s:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        if len(s) == 8:
            return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None
    return None


def _trading_days_between(d_from: date, d_to: date, cap: int = 60):
    """(d_from, d_to] 之间的交易日数。日历不可用返回 None。

    逐日调 rhythm.is_trading_day_now —— 它内部已处理"日历取不到按工作日保守处理"，
    所以这里不要再自行判断周末，否则两套规则会在边界上打架。
    """
    if d_from is None or d_to is None:
        return None
    if d_to <= d_from:
        return 0
    if (d_to - d_from).days > cap:
        return None  # 差太远不逐日算，标 None 由调用方显示"超过 N 日"
    try:
        from viewer.rhythm import is_trading_day_now
    except Exception:  # noqa: BLE001
        return None
    n = 0
    d = d_from + timedelta(days=1)
    while d <= d_to:
        try:
            if is_trading_day_now(d.strftime("%Y-%m-%d")):
                n += 1
        except Exception:  # noqa: BLE001
            return None
        d += timedelta(days=1)
    return n


# ── 写入：唯一写入口 ──────────────────────────────────────────────────────────
def ensure_table(con: sqlite3.Connection) -> None:
    con.executescript(_DDL)
    con.commit()


def record_job_run(job: str, ok: bool, started: float = None, ended: float = None,
                   rc: int = 0, error: str = "", con: sqlite3.Connection = None) -> bool:
    """记录一次任务运行。失败**不抛**——记录本身坏掉不能连累任务结果。

    返回是否写成功。写失败只打印，不改变调用方的退出码。
    """
    own = con is None
    try:
        if own:
            con = sqlite3.connect(APP_DB, timeout=20)
            con.execute("PRAGMA busy_timeout=20000")
        ensure_table(con)
        now = datetime.now()
        st = started if started is not None else time.time()
        en = ended if ended is not None else time.time()
        con.execute(
            "INSERT INTO job_runs(job,started,ended,elapsed,ok,rc,error,created)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (job,
             datetime.fromtimestamp(st).strftime("%Y-%m-%d %H:%M:%S"),
             datetime.fromtimestamp(en).strftime("%Y-%m-%d %H:%M:%S"),
             round(max(0.0, en - st), 2),
             1 if ok else 0, int(rc), (error or "")[:2000],
             now.strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        return True
    except sqlite3.Error as e:
        print("[health] 任务记录写入失败（不影响任务本身）：%s" % e)
        return False
    finally:
        if own and con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


# ── 读取：任务健康 ────────────────────────────────────────────────────────────
def job_health(days: int = 7, con: sqlite3.Connection = None) -> dict:
    """每个任务的最近一次运行 + 连续失败次数。

    `consecutive_fail` 是这里最该看的一个数：单次失败大概率是网络抖动，
    连续失败才是"这个任务坏了"。它按 ended 倒序数到第一次成功为止。
    """
    own = con is None
    out = {"ok": True, "days": days, "jobs": [], "n_jobs": 0,
           "failing": [], "note": ""}
    try:
        if own:
            con = sqlite3.connect(APP_DB, timeout=20)
            con.execute("PRAGMA busy_timeout=20000")
        ensure_table(con)
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = con.execute(
            "SELECT job, ended, elapsed, ok, rc, error FROM job_runs"
            " WHERE ended >= ? ORDER BY job, ended DESC", (since,)).fetchall()
        cur = {}
        for job, ended, elapsed, ok, rc, err in rows:
            if job not in cur:
                cur[job] = {"job": job, "last_ended": ended, "last_elapsed": elapsed,
                            "last_ok": bool(ok), "last_rc": rc,
                            "last_error": (err or "")[:300],
                            "runs": 0, "fails": 0, "consec_fail": 0, "_streak": True}
            j = cur[job]
            j["runs"] += 1
            if not ok:
                j["fails"] += 1
                if j["_streak"]:
                    j["consec_fail"] += 1
            else:
                j["_streak"] = False
        for j in cur.values():
            j.pop("_streak", None)
            # 连续失败 ≥3 才算"坏了"；1-2 次留给重试机制自己消化
            j["broken"] = j["consec_fail"] >= 3
            j["degraded"] = (not j["broken"]) and j["consec_fail"] >= 1
        jobs = sorted(cur.values(), key=lambda x: (-x["consec_fail"], x["job"]))
        out["jobs"] = jobs
        out["n_jobs"] = len(jobs)
        out["failing"] = [j["job"] for j in jobs if j["broken"]]
        if not jobs:
            out["note"] = ("最近 %d 天没有任务运行记录 —— 要么任务没跑，"
                           "要么运行入口还没接上记录（tools/sched_job.py）。" % days)
        return out
    except sqlite3.Error as e:
        return {"ok": False, "error": "read_failed", "detail": str(e), "jobs": []}
    finally:
        if own and con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


# ── 读取：数据新鲜度 ──────────────────────────────────────────────────────────
def data_freshness(today: date = None) -> dict:
    """各关键数据的最后日期 + 相对今天的交易日滞后。

    状态三档：
      fresh   滞后 ≤ 容忍值
      stale   滞后 > 容忍值（该更新没更新）
      unknown 读不到表 / 日期解析失败 / 日历不可用 —— **不当成 fresh**
    """
    today = today or date.today()
    items, worst = [], "fresh"
    for name, label, which, sql, tol in TARGETS:
        con = _conn(which)
        if con is None:
            items.append({"key": name, "label": label, "last": None, "lag": None,
                          "status": "unknown", "detail": "库文件不存在"})
            worst = "unknown" if worst == "fresh" else worst
            continue
        try:
            raw = con.execute(sql).fetchone()
            raw = raw[0] if raw else None
        except sqlite3.Error as e:
            items.append({"key": name, "label": label, "last": None, "lag": None,
                          "status": "unknown", "detail": "查询失败：%s" % str(e)[:80]})
            worst = "unknown" if worst == "fresh" else worst
            con.close()
            continue
        con.close()
        d = _norm_date(raw)
        if d is None:
            items.append({"key": name, "label": label, "last": str(raw), "lag": None,
                          "status": "unknown", "detail": "日期为空或无法解析：%r" % (raw,)})
            worst = "unknown" if worst == "fresh" else worst
            continue
        lag = _trading_days_between(d, today)
        if lag is None:
            status, detail = "unknown", "交易日历不可用或跨度过大，无法计算滞后"
            worst = "unknown" if worst == "fresh" else worst
        elif lag <= tol:
            status, detail = "fresh", "滞后 %d 个交易日（容忍 %d）" % (lag, tol)
        else:
            status, detail = "stale", "滞后 %d 个交易日，超过容忍 %d" % (lag, tol)
            if worst != "stale":
                worst = "stale"
        items.append({"key": name, "label": label, "last": d.isoformat(),
                      "lag": lag, "tolerance": tol, "status": status, "detail": detail})
    return {"ok": True, "asof": today.isoformat(), "items": items, "worst": worst,
            "basis": "滞后按交易日计算（rhythm.is_trading_day_now），基准是今天而非内部最新日期"}


# 缓存：健康检查会被面板轮询，而 kline 的 MAX(d) 虽然是 covering index，
# 在 1.7 亿行的库上仍要 0.59s。新鲜度本身按交易日变，1 分钟内不可能变两次。
_CACHE = {"t": 0.0, "data": None}
_CACHE_TTL = 60.0


def overall(today: date = None, use_cache: bool = True) -> dict:
    """把任务健康与数据新鲜度合成一个总状态。

    总状态取最差的一档：stale > unknown > degraded > fresh。
    `unknown` 排在 stale 之后，是因为"不知道"不该被当成"没事"。
    """
    if use_cache and today is None:
        now = time.time()
        if _CACHE["data"] is not None and now - _CACHE["t"] < _CACHE_TTL:
            return _CACHE["data"]
    data = _overall_uncached(today)
    if today is None:
        _CACHE["data"] = data
        _CACHE["t"] = time.time()
    return data


def _overall_uncached(today: date = None) -> dict:
    jh = job_health()
    fr = data_freshness(today)
    level = "fresh"
    reasons = []
    if fr.get("worst") == "stale":
        level = "stale"
        reasons += ["%s 已过期（%s）" % (i["label"], i["detail"])
                    for i in fr.get("items", []) if i["status"] == "stale"]
    elif fr.get("worst") == "unknown":
        level = "unknown"
        reasons += ["%s 状态不明" % i["label"]
                    for i in fr.get("items", []) if i["status"] == "unknown"]
    if jh.get("ok") and jh.get("failing"):
        if level == "fresh":
            level = "degraded"
        reasons += ["任务连续失败：%s" % "、".join(jh["failing"])]
    elif jh.get("ok") and not jh.get("jobs"):
        if level == "fresh":
            level = "unknown"
        reasons.append("最近 7 天没有任何任务运行记录")
    elif not jh.get("ok") and level == "fresh":
        level = "unknown"
        reasons.append("任务记录读取失败")
    return {"ok": True, "level": level, "reasons": reasons,
            "jobs": jh, "freshness": fr,
            "describe": {
                "level": {"fresh": "全部正常", "degraded": "任务有失败",
                          "stale": "数据过期", "unknown": "状态不明（不等于正常）"},
                "job_consec_fail": "连续失败次数；单次失败可能是抖动，≥3 次判为 broken",
                "freshness": "滞后按交易日算；unknown 表示判不出，不当 fresh",
            }}


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="系统健康汇总（只读）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    o = overall()
    if a.json:
        print(json.dumps(o, ensure_ascii=False, indent=1))
    else:
        print("总状态：%s（%s）" % (o["level"], o["describe"]["level"][o["level"]]))
        for r in o["reasons"]:
            print("  · " + r)
        print("\n数据新鲜度：")
        for i in o["freshness"]["items"]:
            print("  %-22s %-12s %-8s %s"
                  % (i["label"], i["last"] or "—", i["status"], i["detail"]))
        jh = o["jobs"]
        print("\n调度任务（最近 %d 天）：%d 个" % (jh.get("days", 7), jh.get("n_jobs", 0)))
        for j in jh.get("jobs", [])[:12]:
            print("  %-26s last=%s %s 失败%d/%d 连失%d%s"
                  % (j["job"], j["last_ended"], "OK" if j["last_ok"] else "FAIL",
                     j["fails"], j["runs"], j["consec_fail"],
                     "  [BROKEN]" if j["broken"] else ""))
        if not jh.get("jobs"):
            print("  " + (jh.get("note") or ""))
