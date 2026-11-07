"""开机补跑（debate_autorun）离线单测。

不碰网络、不调模型、不读生产库：用内存 SQLite 造最小 schema，
把「有无 Key」「最新交易日」替换掉，只验判定逻辑与触发行为。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from viewer import debate_autorun as da


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE ai_debate (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_date TEXT, provider TEXT, mode TEXT, run_key TEXT,
            status TEXT, pool INTEGER, produced INTEGER, calls INTEGER,
            elapsed_ms INTEGER, result_json TEXT, trace_json TEXT,
            note TEXT, created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE stock_indicators_daily (
            date TEXT, code TEXT, close REAL
        );
    """)
    yield c
    c.close()


def _seed_coverage(conn, date: str, n: int = 3000):
    conn.executemany("INSERT INTO stock_indicators_daily (date, code, close) VALUES (?,?,?)",
                     [(date, "%06d" % i, 10.0) for i in range(n)])
    conn.commit()


def _seed_ok_batch(conn, date: str):
    conn.execute(
        "INSERT INTO ai_debate (review_date, provider, mode, run_key, status, "
        "produced, calls, elapsed_ms) VALUES (?,?,?,?,?,?,?,?)",
        (date, "deepseek", "batch", "", "ok", 18, 101, 111000))
    conn.commit()


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    """默认：有 Key、开关开、上限 2 次；latest_trade_date 由用例通过 _dates 指定。"""
    monkeypatch.setattr(da, "ENABLED", True, raising=False)
    monkeypatch.setattr(da, "MAX_TRIES", 2, raising=False)
    monkeypatch.setattr(da, "MIN_COVERAGE", 3000, raising=False)
    monkeypatch.setattr(da, "RETRY_WAIT", 1200, raising=False)
    monkeypatch.setattr(da, "has_key", lambda: True, raising=False)
    monkeypatch.setattr(da, "DELAY", 0, raising=False)
    monkeypatch.setattr(da, "TICK", 1, raising=False)


def _set_date(monkeypatch, date: str):
    monkeypatch.setattr(da, "_latest_trade_date", lambda conn: date, raising=False)


# ---------------------------------------------------------------- 判定

def test_skips_when_already_ran(conn, monkeypatch):
    """今天已经跑过批量 → 绝不重跑（防止开机一次烧一轮钱）。"""
    _set_date(monkeypatch, "2026-11-02")
    _seed_coverage(conn, "2026-11-02")
    _seed_ok_batch(conn, "2026-11-02")

    d = da.decide(conn, now=datetime(2026, 11, 2, 21, 0))

    assert d["run"] is False
    assert d["reason"] == "done:2026-11-02"


def test_skips_when_no_api_key(conn, monkeypatch):
    """没配 Key → 不跑（否则每次开机白烧一次调用，还什么都得不到）。"""
    _set_date(monkeypatch, "2026-11-02")
    _seed_coverage(conn, "2026-11-02")
    monkeypatch.setattr(da, "has_key", lambda: False, raising=False)

    d = da.decide(conn, now=datetime(2026, 11, 2, 21, 0))

    assert d["run"] is False
    assert d["reason"] == "no_key"
    assert d["wait_sec"] == 0          # 终态：傻等没意义，配好 Key 下次启动再判


def test_waits_when_data_not_ready(conn, monkeypatch):
    """抓取没跑完（指标只入库 300 只）→ 不能拿半成品跑，退避后重判。"""
    _set_date(monkeypatch, "2026-11-02")
    _seed_coverage(conn, "2026-11-02", n=300)

    d = da.decide(conn, now=datetime(2026, 11, 2, 21, 0))

    assert d["run"] is False
    assert d["reason"].startswith("data_not_ready")
    assert d["coverage"] == 300
    assert d["wait_sec"] >= 60


def test_waits_before_close_gate_on_same_day(conn, monkeypatch):
    """当天 15:10 前（盘中/刚收盘）不跑，等到 15:10。"""
    _set_date(monkeypatch, "2026-11-02")
    _seed_coverage(conn, "2026-11-02")

    d = da.decide(conn, now=datetime(2026, 11, 2, 10, 0))

    assert d["run"] is False
    assert d["reason"] == "pre_close"
    # 10:00 → 15:10 = 5 小时 10 分 = 18600 秒
    assert d["wait_sec"] == 18600


def test_runs_when_pending(conn, monkeypatch):
    """数据齐、没跑过、已过收盘闸门 → 该跑。"""
    _set_date(monkeypatch, "2026-11-02")
    _seed_coverage(conn, "2026-11-02")

    d = da.decide(conn, now=datetime(2026, 11, 2, 21, 0))

    assert d["run"] is True
    assert d["reason"] == "pending"
    assert d["target_date"] == "2026-11-02"


def test_runs_for_past_trade_date_regardless_of_clock(conn, monkeypatch):
    """目标日是往日（比如周一早上开机，最新交易日仍是上周五且没跑过）→ 立刻补，不等闸门。"""
    _set_date(monkeypatch, "2026-10-30")
    _seed_coverage(conn, "2026-10-30")

    d = da.decide(conn, now=datetime(2026, 11, 2, 8, 0))

    assert d["run"] is True
    assert d["target_date"] == "2026-10-30"


def test_gives_up_after_max_tries(conn, monkeypatch):
    """同一交易日自动跑够上限仍无成功结果 → 放弃（防开机-崩溃-开机无限烧）。"""
    _set_date(monkeypatch, "2026-11-02")
    _seed_coverage(conn, "2026-11-02")
    conn.execute("INSERT INTO meta (key,value) VALUES (?,?)",
                 ("debate_autorun_tries:2026-11-02", "2"))
    conn.commit()

    d = da.decide(conn, now=datetime(2026, 11, 2, 21, 0))

    assert d["run"] is False
    assert d["reason"].startswith("give_up")
    assert d["tries"] == 2


def test_disabled_switch(conn, monkeypatch):
    monkeypatch.setattr(da, "ENABLED", False, raising=False)
    d = da.decide(conn)
    assert d["run"] is False
    assert d["reason"] == "disabled"


# ---------------------------------------------------------------- 触发

def test_drive_fires_once_and_bumps_tries(conn, monkeypatch):
    """drive 该跑时调一次 start_batch，并把该日触发次数 +1。"""
    _set_date(monkeypatch, "2026-11-02")
    _seed_coverage(conn, "2026-11-02")
    hits = []
    da.drive(lambda: hits.append(1) or {"ok": True, "state": "computing"}, conn, once=True)

    assert hits == [1]
    row = conn.execute("SELECT value FROM meta WHERE key=?",
                       ("debate_autorun_tries:2026-11-02",)).fetchone()
    assert row and row[0] == "1"


def test_drive_does_not_fire_when_done(conn, monkeypatch):
    _set_date(monkeypatch, "2026-11-02")
    _seed_coverage(conn, "2026-11-02")
    _seed_ok_batch(conn, "2026-11-02")
    hits = []
    da.drive(lambda: hits.append(1), conn, once=True)

    assert hits == []


def test_drive_survives_start_failure(conn, monkeypatch):
    """触发本身抛异常不能把线程搞崩，也不能把计数写乱（下次启动还能补）。"""
    _set_date(monkeypatch, "2026-11-02")
    _seed_coverage(conn, "2026-11-02")
    def _boom():
        raise RuntimeError("spawn failed")

    da.drive(_boom, conn, once=True)   # 不抛出去
    assert da.decide(conn, now=datetime(2026, 11, 2, 21, 0))["tries"] == 1
