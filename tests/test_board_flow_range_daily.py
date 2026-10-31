# -*- coding: utf-8 -*-
"""`board_flow_range` 日更（board_flow_daily）的回归测试（2026-10-30）。

## 为什么会有这个文件

`board_flow_daily` 停更了 21 个交易日（库内 217 行全落在 2026-10-08）。定位下来是三层叠一起：

1. **没人调**：唯一写入路径 `ensure_daily` 只由「区间资金面板请求」间接触发，
   `tools/sched_job.py` 里没有任何 board_flow 分支。用户不打开那个面板 → 表永不更新。
2. **调了也不抓**：`ensure_daily(force=False)` 的命中闸是恒真式 ——
   `_db_latest` 返回的「时刻」其实是 `time.time()`，于是 `now - fresh[1] < DAY_TTL` 永远成立；
   叠上 `_db_missing_recent(MAX(trade_date))`（从定义上就不可能为真），整个闸门恒真。
   实测库副本：`ensure_daily(force=False)` → 0.000s、**0 个 HTTP 请求**、返回 `cached=True`。
3. **抓了也白抓**：行构造写成 `names.get("", "")`（拿空 key 查字典 → 恒 `""`）→
   217 个板块的 `board_code` 全为空串 → 主键 `(trade_date, kind, board_code)` 塌成
   「一天一行」。实测：写 2 行 → 库里该日 1 行。

本文件逐条钉住这三层，外加日更自身的闸门 / 幂等 / 失败语义。

## 隔离纪律（硬约束，不是洁癖）

**测试绝不允许写生产库。** 三层防线，与 `tests/test_lhb_daily_offproc.py` 同款：
  ① 模块级 `_PROD_DB` / `_PROD_LOG` 快照 + `_prod_write_blocked()` 在 `PYTEST_CURRENT_TEST`
     环境下拦截一切指向生产路径的写；
  ② 本文件的 `bfr` fixture 把 `DB_PATH` / `LOG_FILE` / `_STATE_CONN_FACTORY` 全指到 `tmp_path`；
  ③ `_SYNC_RUNNING` 标志在收尾复位。

时间也全部钉死（`datetime.now()` 被替换为固定的 2026-10-30 20:00，周五），网络全部打桩，
所以这个文件不联网、不依赖"今天是几号"、跑多久结果都一样。
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 固定「现在」：2026-10-30 是周五（交易日），20:00 已过 18:30 窗口。
DAY = "2026-10-30"
PREV_DAY = "2026-10-29"
FAKE_NOW = datetime(2026, 10, 30, 20, 0, 0)

INDUSTRY_ROWS = [("BK0478", "化工原料"), ("BK1033", "电池"), ("BK0727", "医疗服务")]
CONCEPT_ROWS = [("BK0896", "白酒"), ("BK0477", "酿酒概念")]


class _FrozenDateTime(datetime):
    """把模块内的 `datetime.now()` 钉在 FAKE_NOW —— 让闸门用例与真实日期解耦。"""

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return FAKE_NOW


@pytest.fixture()
def bfr(monkeypatch, tmp_path):
    """board_flow_range 指向临时库；时间冻结；网络全桩；状态标志复位。"""
    mod = importlib.import_module("board_flow_range")
    db = str(tmp_path / "app.db")
    monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr(mod, "LOG_FILE", str(tmp_path / "board_flow_range_daily.log"))
    monkeypatch.setattr(mod, "_STATE_CONN_FACTORY", lambda: sqlite3.connect(db, timeout=30))
    monkeypatch.setattr(mod, "datetime", _FrozenDateTime)
    # 交易日判据：只用确定性的「周一~周五」，不联网（节假日另有用例单独打桩）
    monkeypatch.setattr(
        mod, "is_trading_day",
        lambda today=None: datetime.strptime(today or DAY, "%Y-%m-%d").weekday() < 5)
    monkeypatch.setattr(mod, "_SYNC_RUNNING", {"v": False})
    yield mod
    mod._SYNC_RUNNING["v"] = False


def _conn(bfr) -> sqlite3.Connection:
    con = sqlite3.connect(bfr.DB_PATH, timeout=30)
    bfr.ensure_schema(con)
    bfr.ensure_meta_schema(con)
    return con


def _count(bfr, date: str = "", kind: str = "") -> int:
    con = _conn(bfr)
    try:
        sql = "SELECT COUNT(*) FROM board_flow_daily WHERE 1=1"
        args: list = []
        if date:
            sql += " AND trade_date=?"
            args.append(date)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


def _state(bfr) -> dict:
    con = _conn(bfr)
    try:
        return bfr._state_read(con)
    finally:
        con.close()


def _stub_network(bfr, monkeypatch, ok: bool = True):
    """把 `_dc_day_rows` 换成计数桩；`_dc_latest_date` 固定成 DAY。

    返回 `calls`（按调用顺序累积的 (kind, date)）—— 用它证明"没再拉网络"。
    """
    calls: list = []

    def fake_dc(kind, date, page_size=None, max_pages=None):  # noqa: ARG001
        calls.append((kind, date))
        if not ok:
            return [], {"ok": False, "pages": 0, "code": 9201, "reason": "返回数据为空"}
        src = INDUSTRY_ROWS if kind == "industry" else CONCEPT_ROWS
        rows = [(date, kind, c, n, 1.0e8, 1.5, bfr.SRC_DC) for c, n in src]
        return rows, {"ok": True, "pages": 1, "code": 0, "reason": ""}

    monkeypatch.setattr(bfr, "_dc_day_rows", fake_dc)
    monkeypatch.setattr(bfr, "_dc_latest_date", lambda kind: DAY)
    return calls


def _pin_calendar(bfr, monkeypatch, days: list):
    """把「最近 N 个交易日」固定住，避免用例依赖真实交易日历（可能要联网）。"""
    monkeypatch.setattr(bfr, "_recent_trade_days",
                        lambda n=3, end="": list(days)[-max(1, int(n)):])


# ------------------------------------------------------------------ 1. 闸门四态
def test_gate_skips_pre_window(bfr):
    """未到 18:30 → skip: pre_publish（哪怕当天已收盘）。"""
    with _conn(bfr) as con:
        skip, today = bfr._gate(con, now=datetime(2026, 10, 30, 18, 29))
    assert skip == {"skip": "pre_publish"}
    assert today == DAY


def test_gate_allows_at_window_boundary(bfr):
    """18:30 整即放行 —— 边界不通的话，18:30 那一班永远差一分钟。"""
    with _conn(bfr) as con:
        skip, today = bfr._gate(con, now=datetime(2026, 10, 30, 18, 30))
    assert skip is None
    assert today == DAY


def test_gate_skips_weekend(bfr):
    """周末 → skip: weekend（2026-10-31 是周六）。判据是纯 weekday，不碰网络。"""
    with _conn(bfr) as con:
        skip, _today = bfr._gate(con, now=datetime(2026, 10, 31, 20, 0))
    assert skip == {"skip": "weekend"}


def test_gate_skips_non_trading_holiday(bfr, monkeypatch):
    """工作日但休市（长假）→ skip: non_trading_day；日历未知（None）则不拦。"""
    monkeypatch.setattr(bfr, "is_trading_day", lambda today=None: False)
    with _conn(bfr) as con:
        skip, _today = bfr._gate(con, now=datetime(2026, 10, 30, 20, 0))
    assert skip == {"skip": "non_trading_day"}
    monkeypatch.setattr(bfr, "is_trading_day", lambda today=None: None)
    with _conn(bfr) as con:
        skip2, _today = bfr._gate(con, now=datetime(2026, 10, 30, 20, 0))
    assert skip2 is None, "日历未知时不该拦（宁可空跑一次也不漏更）"


def test_gate_skips_done_today(bfr):
    """当日已成功（last_date=today）→ skip: done_today。"""
    con = _conn(bfr)
    try:
        bfr._state_write(con, {"last_date": DAY, "tries": 0})
    finally:
        con.close()
    with _conn(bfr) as con:
        skip, _today = bfr._gate(con, now=datetime(2026, 10, 30, 20, 0))
    assert skip == {"skip": "done_today"}


def test_gate_skips_when_failed_too_many_times(bfr):
    """当日失败达 MAX_TRIES → skip: failed_today（防限流打爆数据源，次日自动恢复）。"""
    con = _conn(bfr)
    try:
        bfr._state_write(con, {"tries": bfr.MAX_TRIES, "tries_date": DAY})
    finally:
        con.close()
    with _conn(bfr) as con:
        skip, _today = bfr._gate(con, now=datetime(2026, 10, 30, 20, 0))
    assert skip == {"skip": "failed_today"}


def test_gate_force_does_not_bypass_idempotency(bfr):
    """`force` 只放宽时间窗/交易日闸，**不许**绕过「当日已成功」。

    否则手工 `--force` 每跑一次就把全表重写一遍，而真正的日更闸门反而更松。
    """
    con = _conn(bfr)
    try:
        bfr._state_write(con, {"last_date": DAY})
    finally:
        con.close()
    with _conn(bfr) as con:
        # 凌晨 3:00 本不满足时间窗 —— force 正是用来跳过时间窗的；但"当日已完成"不许跳
        skip, _today = bfr._gate(con, now=datetime(2026, 10, 30, 3, 0), force=True)
    assert skip == {"skip": "done_today"}, "force 竟绕过了当日幂等"



# ------------------------------------------------------------------ 2. 成功后不再拉网络
def test_second_run_makes_no_network_calls(bfr, monkeypatch):
    """当日成功后再调一次 → 0 个网络请求（闸门在拉数之前就挡住了）。"""
    calls = _stub_network(bfr, monkeypatch)
    _pin_calendar(bfr, monkeypatch, [DAY])
    con = _conn(bfr)
    try:
        first = bfr.run_daily_sync(con, force=True)
        assert first.get("ok") is True, first
        n_first = len(calls)
        assert n_first > 0, "第一次竟然一个请求都没发"
        second = bfr.run_daily_sync(con, force=True)
    finally:
        con.close()
    assert second == {"skip": "done_today"}, second
    assert len(calls) == n_first, "当日已成功却又去拉了网络：%d → %d" % (n_first, len(calls))


def test_success_writes_last_date_and_clears_tries(bfr, monkeypatch):
    """成功轮：写 last_date / last_ok_at / last_data_date，清 tries，两个 kind 都落库。"""
    _stub_network(bfr, monkeypatch)
    _pin_calendar(bfr, monkeypatch, [DAY])
    con = _conn(bfr)
    try:
        res = bfr.run_daily_sync(con, force=True)
    finally:
        con.close()
    assert res["ok"] is True, res
    st = _state(bfr)
    assert st.get("last_date") == DAY
    assert st.get("last_ok_at")
    assert int(st.get("tries", 0)) == 0
    assert st.get("last_data_date") == DAY
    assert _count(bfr, DAY, "industry") == len(INDUSTRY_ROWS)
    assert _count(bfr, DAY, "concept") == len(CONCEPT_ROWS)


# ------------------------------------------------------------------ 3. 失败不许当成功
def test_failure_does_not_write_last_date(bfr, monkeypatch):
    """**这是导致停更复发的那个 bug 形态**：失败轮绝不能写 last_date。

    一旦写了，`_gate` 就判 done_today → 当天不再重试 → 数据静默停更。
    """
    _stub_network(bfr, monkeypatch, ok=False)
    _pin_calendar(bfr, monkeypatch, [DAY])
    con = _conn(bfr)
    try:
        res = bfr.run_daily_sync(con, force=True)
    finally:
        con.close()
    assert res.get("ok") is False, "失败轮必须把 ok=False 交出去，否则父进程记成「今天跑过了」"
    assert res.get("skip") is None, "这一轮是真跑过（只是失败），不该返回 skip"
    st = _state(bfr)
    assert st.get("last_date") != DAY, "失败轮把「今天算完成」写进去了 —— 停更会复发"
    assert int(st.get("tries")) == 1
    assert st.get("tries_date") == DAY
    assert st.get("last_error")
    assert _count(bfr) == 0, "失败轮不该落任何行"


def test_retry_allowed_until_max_tries_then_stops(bfr, monkeypatch):
    """失败后可重试 MAX_TRIES 次，第 MAX_TRIES 次之后才 failed_today；全程不写 last_date。"""
    _stub_network(bfr, monkeypatch, ok=False)
    _pin_calendar(bfr, monkeypatch, [DAY])
    for i in range(1, bfr.MAX_TRIES + 1):
        con = _conn(bfr)
        try:
            res = bfr.run_daily_sync(con, force=True)
        finally:
            con.close()
        assert res.get("ok") is False, "第 %d 次应仍被判失败：%s" % (i, res)
        assert int(_state(bfr).get("tries")) == i
    con = _conn(bfr)
    try:
        last = bfr.run_daily_sync(con, force=True)
    finally:
        con.close()
    assert last == {"skip": "failed_today"}
    assert _state(bfr).get("last_date") != DAY


def test_partial_day_is_not_success(bfr, monkeypatch):
    """只落了一半（某 kind 失败）→ 不算成功：宁可当天再重试，也不记 done。"""

    def fake_dc(kind, date, page_size=None, max_pages=None):  # noqa: ARG001
        if kind == "concept":
            return [], {"ok": False, "pages": 0, "code": 9201, "reason": "返回数据为空"}
        rows = [(date, kind, c, n, 1.0e8, 1.5, bfr.SRC_DC) for c, n in INDUSTRY_ROWS]
        return rows, {"ok": True, "pages": 1, "code": 0, "reason": ""}

    monkeypatch.setattr(bfr, "_dc_day_rows", fake_dc)
    monkeypatch.setattr(bfr, "_dc_latest_date", lambda kind: DAY)
    _pin_calendar(bfr, monkeypatch, [DAY])
    con = _conn(bfr)
    try:
        res = bfr.run_daily_sync(con, force=True)
    finally:
        con.close()
    assert res.get("ok") is False, res
    assert _state(bfr).get("last_date") != DAY
    assert _count(bfr, DAY, "industry") == len(INDUSTRY_ROWS), "industry 那半边应已落库"


# ------------------------------------------------------------------ 4. 幂等：行数不膨胀
def test_repeated_persist_does_not_grow_rows(bfr):
    """同一批数据反复落库 → 行数不变（主键 (trade_date,kind,board_code) + INSERT OR REPLACE）。"""
    con = _conn(bfr)
    try:
        rows = [(DAY, "industry", c, n, 1.0e8, 1.5, bfr.SRC_DC) for c, n in INDUSTRY_ROWS]
        for _ in range(3):
            assert bfr._persist_day(con, "industry", rows) == len(rows)
    finally:
        con.close()
    assert _count(bfr) == len(INDUSTRY_ROWS)


def test_sync_day_twice_keeps_row_count(bfr, monkeypatch):
    """`sync_day` 跑两遍：都 ok，但行数一点不涨（"可反复运行"的判据）。"""
    _stub_network(bfr, monkeypatch)
    con = _conn(bfr)
    try:
        r1 = bfr.sync_day("industry", con, DAY)
        n1 = _count(bfr, DAY, "industry")
        r2 = bfr.sync_day("industry", con, DAY)
        n2 = _count(bfr, DAY, "industry")
    finally:
        con.close()
    assert r1.get("ok") and r2.get("ok"), (r1, r2)
    assert n1 == n2 == len(INDUSTRY_ROWS), "重复落库把行数改了：%s → %s" % (n1, n2)


def test_sync_days_across_two_dates_coexist(bfr, monkeypatch):
    """两个交易日的数据互不覆盖（旧 bug 的另一个侧面：主键塌陷会让后一天顶掉前一天）。"""
    _stub_network(bfr, monkeypatch)
    con = _conn(bfr)
    try:
        bfr.sync_day("industry", con, PREV_DAY)
        bfr.sync_day("industry", con, DAY)
    finally:
        con.close()
    assert _count(bfr, PREV_DAY, "industry") == len(INDUSTRY_ROWS)
    assert _count(bfr, DAY, "industry") == len(INDUSTRY_ROWS)


# ------------------------------------------------------------------ 5. 历史上那三个 bug 各钉一条
def test_ensure_daily_writes_real_board_code(bfr, monkeypatch):
    """`ensure_daily` 的行必须带**真实 BK 码**。

    原实现写的是 `names.get("", "")` —— 拿空 key 查字典，恒等于 `""` → 主键塌成「一天一行」。
    实测：写 2 行 → 库里 1 行、board_code=''。这条用例就是那个形状的回归。
    """
    monkeypatch.setattr(bfr, "_board_targets", lambda kind, limit=0: [("BK0478", "化工原料")])
    monkeypatch.setattr(bfr, "_em_daykline", lambda code, day_max=400: [
        (DAY, 1.0e8, 1.5), (PREV_DAY, -2.0e8, -2.5)])
    con = _conn(bfr)
    try:
        res = bfr.ensure_daily("industry", force=True, conn=con)
        codes = [r[0] for r in con.execute(
            "SELECT board_code FROM board_flow_daily WHERE trade_date=?", (DAY,)).fetchall()]
    finally:
        con.close()
    assert res.get("ok") is True, res
    assert codes == ["BK0478"], "board_code 又写成空串了：%r" % (codes,)


def test_ensure_daily_is_not_a_tautology(bfr, monkeypatch):
    """`ensure_daily(force=False)` 在「库里只有旧数据」时必须**真的去抓**。

    原实现的命中闸是恒真式（`_db_latest` 的"时刻"其实是 time.time()）→ 永远 0 请求返回 cached。
    """
    con = _conn(bfr)
    try:
        bfr._persist_day(con, "industry", [
            ("2026-10-08", "industry", "BK0478", "化工原料", 1.0e8, 1.5, bfr.SRC_DC)])
    finally:
        con.close()

    calls = {"n": 0}

    def counting(code, day_max=400):  # noqa: ARG001
        calls["n"] += 1
        return [(DAY, 3.0e8, 1.0)]

    monkeypatch.setattr(bfr, "_board_targets", lambda kind, limit=0: [("BK0478", "化工原料")])
    monkeypatch.setattr(bfr, "_em_daykline", counting)
    monkeypatch.setattr(bfr, "latest_trade_date", lambda: DAY)
    monkeypatch.setattr(bfr, "_MEM", {})

    con = _conn(bfr)
    try:
        res = bfr.ensure_daily("industry", force=False, conn=con)
    finally:
        con.close()
    assert calls["n"] > 0, (
        "库里最新日 2026-10-08 已落后 15 个交易日，却一个请求都没发（恒真闸门复现）：%s" % (res,))
    assert res.get("cached") is not True


def test_ensure_daily_returns_cached_when_actually_fresh(bfr, monkeypatch):
    """反过来也要对：库里已覆盖最近交易日 → 命中缓存、不抓（别把修复做成"每次都全量抓"）。"""
    con = _conn(bfr)
    try:
        bfr._persist_day(con, "industry", [
            (DAY, "industry", "BK0478", "化工原料", 1.0e8, 1.5, bfr.SRC_DC)])
    finally:
        con.close()
    calls = {"n": 0}

    def counting(code, day_max=400):  # noqa: ARG001
        calls["n"] += 1
        return [(DAY, 3.0e8, 1.0)]

    monkeypatch.setattr(bfr, "_board_targets", lambda kind, limit=0: [("BK0478", "化工原料")])
    monkeypatch.setattr(bfr, "_em_daykline", counting)
    monkeypatch.setattr(bfr, "latest_trade_date", lambda: DAY)
    monkeypatch.setattr(bfr, "_MEM", {})
    con = _conn(bfr)
    try:
        res = bfr.ensure_daily("industry", force=False, conn=con)
    finally:
        con.close()
    assert calls["n"] == 0, "数据已覆盖最近交易日却仍去抓了 %d 次" % calls["n"]
    assert res.get("cached") is True and res.get("latest") == DAY


def test_ensure_daily_reports_failure_when_source_is_throttled(bfr, monkeypatch):
    """所有板块都抓空 → `ok=False`（原实现写死 True，失败还被缓存 6 小时）。

    实测背景：东财 fflow/daykline 对本机密集请求会返回空响应（0.5s / None）。
    """
    monkeypatch.setattr(bfr, "_board_targets", lambda kind, limit=0: [("BK0478", "化工原料")])
    monkeypatch.setattr(bfr, "_em_daykline", lambda code, day_max=400: None)
    monkeypatch.setattr(bfr, "latest_trade_date", lambda: DAY)
    monkeypatch.setattr(bfr, "_MEM", {})
    con = _conn(bfr)
    try:
        res = bfr.ensure_daily("industry", force=True, conn=con)
    finally:
        con.close()
    assert res.get("ok") is False, "全部板块抓空却回报成功：%s" % (res,)
    assert res.get("reason")


# ------------------------------------------------------------------ 6. 结构约束 / 接线
def test_run_daily_sync_is_synchronous(bfr):
    """调度入口里不许出现 `Thread(`：短命子进程退出时 daemon 线程会被腰斩。

    lhb_daily 为此骗了 11 天（191 次 `{'started'}`、每次 0.1s），绝不能复刻。
    """
    for fn in (bfr.run_daily_sync, bfr.maybe_daily):
        src = inspect.getsource(fn)
        assert "Thread(" not in src, "%s 里起了线程：短命子进程里会被杀掉" % fn.__name__


def test_sched_job_registers_the_job():
    """`tools/sched_job.py` 必须真的登记这个任务，否则服务端派了进程也跑不了。"""
    spec = importlib.util.spec_from_file_location(
        "sched_job_under_test", str(ROOT / "tools" / "sched_job.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "board_flow_range_daily" in mod.JOBS
    assert callable(mod.JOBS["board_flow_range_daily"])


def test_brake_classifies_job_as_fetch():
    """刹车闸里要显式登记成 FETCH；未登记会吃默认 (COMPUTE, "app")，类别判断就不准。"""
    from viewer.kernel import brake
    kind, domain = brake.profile("board_flow_range_daily")
    assert (kind, domain) == (brake.FETCH, "app")


def test_server_window_matches_module_window(bfr):
    """服务端 `_daily_job` 的时间窗与子进程 `_gate` 的时间窗**必须一致**。

    这是本项目踩过的坑（"手工跑得成、调度跑不成"）的又一变种：
    服务端若在 18:30 派进程、而子进程闸门认为"未到窗口"→ 返回 `skip: pre_publish`
    → 退出码 0 → 父进程补写「今天已完成」→ **当天真的不跑了**，且日志里只有一行 skip。
    这里不复用「导入 server 再读函数」的做法（导入 server 会建 DB 连接、太重），
    直接对源码做文本比对 —— 够用，且不会因为 import 副作用把测试环境搞脏。
    """
    src = (ROOT / "viewer" / "server.py").read_text(encoding="utf-8")
    expect = '_daily_job("board_flow_range_daily", (%d, %d))' % (bfr.PUBLISH_HOUR, bfr.PUBLISH_MIN)
    assert expect in src, (
        "服务端窗口与 board_flow_range._gate 的窗口不一致（期望 %r）。"
        "两处必须同步改，否则调度会静默空转。" % expect)



def test_prod_write_blocked_under_pytest(bfr, monkeypatch):
    """pytest 环境下指向生产路径的写必须被拦下（D33 事故的同款防线）。

    做法：把模块的 `DB_PATH` / `LOG_FILE` 指回**生产路径**（正是事故当时的形态），
    然后调 `record_result` —— 它必须在碰任何文件之前就返回 False，
    生产库里的 `board_flow_range_daily_state` 与生产日志的大小都不许变化。
    """
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_board_flow_range_daily.py::x")
    assert bfr._prod_write_blocked(bfr._PROD_DB, bfr._PROD_DB) is True
    assert bfr._prod_write_blocked(bfr.DB_PATH, bfr._PROD_DB) is False

    prod_state_before = _prod_state()
    prod_log_before = _prod_log_size()
    monkeypatch.setattr(bfr, "DB_PATH", bfr._PROD_DB)
    monkeypatch.setattr(bfr, "LOG_FILE", bfr._PROD_LOG)
    con = _conn(bfr)       # 这个连接仍是 tmp 库：真被拦下就不会有任何写发生
    try:
        blocked = bfr.record_result(con, {"ok": True, "target": DAY}, DAY)
    finally:
        con.close()
    assert blocked is False, "生产库的 state 写没有被拦下"
    assert _prod_state() == prod_state_before, "生产 meta 被测试改写了"
    assert _prod_log_size() == prod_log_before, "生产日志被测试追加了"


def _prod_state():
    """只读读生产库的 board_flow_range_daily_state（None 也算一种确定值）。"""
    con = sqlite3.connect("file:%s?mode=ro" % (ROOT / "viewer" / "app.db").as_posix(), uri=True)
    try:
        row = con.execute(
            "select value from meta where key='board_flow_range_daily_state'").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def _prod_log_size() -> int:
    p = ROOT / "viewer" / "logs" / "board_flow_range_daily.log"
    return p.stat().st_size if p.exists() else -1

