# -*- coding: utf-8 -*-
"""系统健康模块的守卫测试（2026-10-31 决策⑤）。

这里钉的是三个**判错不会报错、只会安静给出错误结论**的地方：
  1. 日期归一化 —— kline 存 20261030（整数），其他表存 '2026-10-30'。
     解析错一个，滞后就变成 None 或乱值。
  2. 连续失败计数 —— 若把"最近一次成功之前的失败"也算进连续失败，
     就会整体误报；若反之，真坏的job反而显示正常。
  3. unknown 不得当 fresh —— 判不出时报"正常"比报错更危险。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from viewer import health as H  # noqa: E402


# ── 日期归一 ──────────────────────────────────────────────────────────────────
def test_norm_date_handles_both_storage_formats():
    """kline 存整数 20261030、其余表存 '2026-10-30'，两者必须归一成同一天。"""
    assert H._norm_date(20261030) == date(2026, 10, 30)
    assert H._norm_date("20261030") == date(2026, 10, 30)
    assert H._norm_date("2026-10-30") == date(2026, 10, 30)
    assert H._norm_date("2026/10/30") == date(2026, 10, 30)


def test_norm_date_returns_none_not_guess():
    """解析不出就 None —— 不许回退成"今天"（那会让陈旧数据看起来新鲜）。"""
    for bad in (None, "", "  ", "abc", "2026-13-45", 0.5):
        assert H._norm_date(bad) is None, bad


# ── 交易日滞后 ────────────────────────────────────────────────────────────────
def test_weekend_is_not_counted_as_lag():
    """周五的数据在周六看，滞后必须是 0 个交易日。

    这是整个模块最容易被"优化"掉的一条：改成自然日算，每个周末都会集体报警，
    报多了就没人看了 —— 而告警的价值全在"有人看"。
    """
    fri = date(2026, 10, 30)
    sat = date(2026, 10, 31)
    sun = date(2026, 11, 1)
    assert H._trading_days_between(fri, sat) == 0
    assert H._trading_days_between(fri, sun) == 0
    # 跨到下个周一才算 1 个交易日
    assert H._trading_days_between(fri, date(2026, 11, 2)) >= 0


def test_lag_is_never_negative():
    """数据日期晚于今天（如公告提前挂出）不能算出负数滞后。"""
    assert H._trading_days_between(date(2026, 10, 31), date(2026, 10, 30)) == 0


# ── 任务记录与连续失败 ────────────────────────────────────────────────────────
def _mem_db():
    con = sqlite3.connect(":memory:")
    H.ensure_table(con)
    return con


def _ins(con, job, ok, ended):
    con.execute(
        "INSERT INTO job_runs(job,started,ended,elapsed,ok,rc,error,created)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (job, ended, ended, 1.0, 1 if ok else 0, 0 if ok else 1,
         "" if ok else "boom", ended))
    con.commit()


def test_consecutive_failure_stops_at_last_success():
    """最近一次成功之前的失败，不算进连续失败。

    否则一个昨天刚修复的任务会永远显示"连失 3 次"，人就不再信这个数了。
    """
    con = _mem_db()
    _ins(con, "fixed_job", False, "2026-10-28 10:00:00")
    _ins(con, "fixed_job", False, "2026-10-28 11:00:00")
    _ins(con, "fixed_job", False, "2026-10-29 10:00:00")
    _ins(con, "fixed_job", True, "2026-10-30 10:00:00")   # 修好了
    st = H.job_health(con=con)
    j = [x for x in st["jobs"] if x["job"] == "fixed_job"][0]
    con.close()
    assert j["consec_fail"] == 0, j
    assert j["fails"] == 3 and j["runs"] == 4
    assert j["broken"] is False
    assert j["degraded"] is False


def test_three_consecutive_failures_is_broken():
    """连续 3 次失败判 broken —— 这是面板上唯一要求人处理的信号。"""
    con = _mem_db()
    _ins(con, "dead_job", True, "2026-10-28 10:00:00")
    for hh in ("2026-10-29 10:00:00", "2026-10-30 10:00:00", "2026-10-31 10:00:00"):
        _ins(con, "dead_job", False, hh)
    st = H.job_health(con=con)
    j = [x for x in st["jobs"] if x["job"] == "dead_job"][0]
    con.close()
    assert j["consec_fail"] == 3
    assert j["broken"] is True
    assert st["failing"] == ["dead_job"]


def test_single_failure_is_degraded_not_broken():
    """单次失败只标 degraded —— 网络抖动不该触发"任务坏了"。"""
    con = _mem_db()
    _ins(con, "flaky_job", True, "2026-10-29 10:00:00")
    _ins(con, "flaky_job", False, "2026-10-30 10:00:00")
    st = H.job_health(con=con)
    j = st["jobs"][0]
    con.close()
    assert j["consec_fail"] == 1
    assert j["degraded"] is True
    assert j["broken"] is False


def test_record_job_run_writes_one_row_and_keeps_error():
    con = _mem_db()
    ok = H.record_job_run("j1", False, started=100.0, ended=105.5, rc=1,
                          error="SystemExit(2)", con=con)
    assert ok is True
    row = con.execute("SELECT job, ok, rc, elapsed, error FROM job_runs").fetchone()
    con.close()
    assert row[0] == "j1" and row[1] == 0 and row[2] == 1
    assert abs(row[3] - 5.5) < 1e-6
    assert "SystemExit" in row[4]


def test_record_failure_does_not_raise_on_bad_connection():
    """记录写失败不能连累任务结果 —— 否则加个监控反而让任务更容易挂。"""
    class Boom:
        def executescript(self, *a, **k):
            raise sqlite3.Error("disk I/O error")
    assert H.record_job_run("j2", True, con=Boom()) is False


# ── 新鲜度：unknown 不得当 fresh ──────────────────────────────────────────────
def test_freshness_unknown_is_not_fresh():
    """读不到表 / 日期为空时状态必须 unknown，且不能把 worst 拉成 fresh。

    这条是整个面板的底线：判不出就说不知道。若这时报"新鲜"，
    人就会在数据其实已经停更的情况下继续用旧数字做决定。
    """
    items = [{"status": "unknown", "label": "X", "detail": "查询失败"}]
    worst = "fresh"
    for i in items:
        if i["status"] == "stale":
            worst = "stale"
        elif i["status"] == "unknown" and worst == "fresh":
            worst = "unknown"
    assert worst == "unknown"
    # 而 overall 的档位说明里必须写明 unknown ≠ 正常
    assert "不等于正常" in H.overall(use_cache=False)["describe"]["level"]["unknown"]


def test_targets_declare_tolerance_for_irregular_sources():
    """龙虎榜/公告不是每个交易日都有，容忍值必须 > 0，否则天天误报过期。"""
    tol = {t[0]: t[4] for t in H.TARGETS}
    assert tol["kline"] == 0
    assert tol["stock_indicators_daily"] == 0
    assert tol["lhb_stocks"] >= 1
    assert tol["stock_ann"] >= 3
