# -*- coding: utf-8 -*-
"""D33 回归：`bili_scan` 的收盘自动扫描（A1/A2/A3 三处缺陷）。

## 三个缺陷（2026-10-30 定位，用 sys.settrace 跟过实际执行行）

**A1 —— `maybe_daily()` 缺 `global _THREAD`，永远抛 UnboundLocalError**
函数体里有 `_THREAD = threading.Thread(...)` 赋值 → `_THREAD` 成了**局部名** → 冒号前那句
`if _THREAD and _THREAD.is_alive()` 直接
`UnboundLocalError: cannot access local variable '_THREAD' where it is not associated with a value`，
被 `except Exception: return False` 吞掉。于是 `sched-bili_scan.log` 里每 15 分钟一条
`[bili_scan] ok 0.2s False` 看着像"当日已完成所以跳过"，其实**这个入口从上线起一次都没进来过**。

**A2 —— `maybe_prewarm()._go()` 同一个病**：服务启动 45 秒后的预热线程同样没建出来。

**A3 —— 就算补上 global，它仍是"起 daemon 线程就 return"**
调用方 `tools/sched_job.py::_bili_scan()` 是**短命子进程**，print 完就退出，CPython 关解释器时
把 daemon 线程杀掉 → 一次都扫不成（与 `lhb_daily` 同一个病）。

修法：`global _THREAD` + 单飞 + 建线程收进 `_start_scan_thread()` 一处；调度路径改同步
`run_daily_sync()`；`maybe_daily()` 降为返回 bool 的兼容别名。

本文件全部用桩（`_run_scan` / `_last_trade_date` / `_get_state` / `_result_cache_load`），
不联网、不读生产库、不起真实扫描。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TODAY = time.strftime("%Y-%m-%d")


@pytest.fixture()
def bs(monkeypatch):
    """干净的 bili_scan：无在跑线程、闸放行、`_run_scan` 打桩。

    收尾那三行是**事故防线**（2026-10-30 15:31）而不是洁癖：如果将来又出现
    "起 daemon 线程就 return" 的回归，孤儿线程会活过 monkeypatch 撤销、拿着**生产**路径
    把结果写进真实缓存。join 必须在 `yield` 之后、monkeypatch 撤销**之前**跑完 ——
    此时线程看到的还是 tmp/替身路径。
    """
    mod = importlib.import_module("bili_scan")
    mod._THREAD = None
    mod._STATE.update({"status": "idle", "phase": "", "done": 0, "total": 0,
                       "note": "", "error": None, "result": None})
    monkeypatch.setattr(mod, "_last_trade_date", lambda: TODAY)
    monkeypatch.setattr(mod, "_result_cache_load", lambda: None)
    # 日更窗口默认关掉（`()` = 不设窗、恒放行）：否则 19:45–23:00 之外跑测试会被
    # `skip: day_brake_window` 整片跳过，变成"结果取决于你几点执行"。
    # 窗口本身由 `test_never_competes_with_lhb_at_1800` / `test_daily_window_edges` 显式测。
    monkeypatch.setattr(mod, "_DAILY_WINDOW", ())
    yield mod
    for t in threading.enumerate():
        if t.name == "bili-scan":
            t.join(timeout=10)
    assert not [t for t in threading.enumerate() if t.name == "bili-scan"], \
        "测试遗留了 bili-scan 线程：它会在 monkeypatch 撤销后跑真实扫描、写生产缓存"
    mod._THREAD = None


def _stub_run_scan(monkeypatch, bs, ok: bool = True, seconds: float = 0.0):
    """把 `_run_scan` 换成桩，返回调用记录（用于断言"返回时活已干完"）。"""
    calls: list = []

    def fake(force):
        if seconds:
            time.sleep(seconds)
        calls.append(force)
        return {"ok": ok, "elapsed": seconds, "hits": 7, "scanned": 100,
                "error": None if ok else "模拟失败"}

    monkeypatch.setattr(bs, "_run_scan", fake)
    return calls


# ---------------------------------------------------------------- A1/A2 结构性护栏
def test_every_function_that_writes_thread_declares_global(bs):
    """任何给 `_THREAD` 赋值的函数都必须写 `global _THREAD`。

    这条用 AST 扫全模块 —— A1/A2 的本质就是"赋值让它变局部名"，人会忘，机器不会。
    """
    tree = ast.parse(inspect.getsource(bs))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        stores = [n for n in ast.walk(node)
                  if isinstance(n, ast.Name) and n.id == "_THREAD"
                  and isinstance(n.ctx, ast.Store)]
        if not stores:
            continue
        globs = [n for n in ast.walk(node)
                 if isinstance(n, ast.Global) and "_THREAD" in n.names]
        if not globs:
            offenders.append(node.name)
    assert offenders == [], "这些函数赋值了 _THREAD 却没声明 global（UnboundLocalError 会重演）：%s" % offenders


def test_start_scan_thread_actually_starts(bs, monkeypatch):
    """`_start_scan_thread` 不再抛 UnboundLocalError，且单飞生效。"""
    _stub_run_scan(monkeypatch, bs, seconds=0.05)
    assert bs._start_scan_thread(False) is True, "_start_scan_thread 没启动线程"
    assert bs._THREAD is not None and bs._THREAD.is_alive()
    assert bs._start_scan_thread(False) is False, "单飞失效：同一时刻起了两个扫描"
    bs._THREAD.join(timeout=3)


def test_maybe_prewarm_can_start_thread(bs, monkeypatch):
    """A2 复现点：`maybe_prewarm()` 的内层 `_go` 曾因缺 global 建不出线程。

    把 `bili_scan.time` 换成「sleep 空实现」的替身，让 45 秒等待立即返回，
    然后**轮询** `_THREAD`（内层是线程，不能假设瞬时）。

    时钟固定在**早盘 09:00**（不是真时钟）：`_go` 现在要区分「昨晚没跑成」和「今天日更还没
    到点」，用真时钟跑就会变成"结果取决于你几点执行"。
    """
    _stub_run_scan(monkeypatch, bs, seconds=0.05)
    _fake_clock(bs, monkeypatch, 9, 0)

    bs.maybe_prewarm()
    # 注意：`_start_scan_thread` 是「先赋值 _THREAD、再 start()」，中间有极短窗口，
    # 所以不能只等 `_THREAD is not None`（会 join 到还没 start 的线程 → RuntimeError），
    # 要等 `ident` 出现（= start() 已执行）。
    deadline = time.time() + 3.0
    while time.time() < deadline and (bs._THREAD is None or bs._THREAD.ident is None):
        time.sleep(0.02)
    assert bs._THREAD is not None, "maybe_prewarm 之后 _THREAD 仍是 None（A2 未修）"
    assert bs._THREAD.ident is not None, "线程对象建了但 start() 没执行"
    bs._THREAD.join(timeout=3)


# ---------------------------------------------------------------- A3 同步语义
def test_run_daily_sync_finishes_before_returning(bs, monkeypatch):
    """核心判据：`run_daily_sync()` **返回那一刻**活已干完（不是"起了线程就走"）。"""
    # 2026-10-31 修：这条原来吃**真实墙钟**。闸要求「15 点后」，于是在上午跑测试必红，
    # 而代码一行没动 —— 把"时间"变成被测对象的输入，测试才不随日历和钟点变色。
    _fake_clock(bs, monkeypatch, 20, 0)
    calls = _stub_run_scan(monkeypatch, bs, seconds=0.2)
    t0 = time.time()
    res = bs.run_daily_sync()
    elapsed = time.time() - t0
    assert calls == [False], "扫描没跑，或跑得不是同步路径"
    assert elapsed >= 0.2, "返回得太快，说明没等扫描跑完：%.3fs" % elapsed
    assert res["ok"] is True
    assert res["hits"] == 7 and res["scanned"] == 100
    assert "elapsed" in res


def test_run_daily_sync_no_thread_in_scheduling_path(bs):
    """结构护栏：调度入口里不许再出现 `Thread(`。"""
    for fn in (bs.run_daily_sync, bs._daily_gate, bs.maybe_daily):
        src = inspect.getsource(fn)
        assert "Thread(" not in src, "%s 里又起线程了，短命子进程里会被腰斩" % fn.__name__


def test_run_daily_sync_reports_failure(bs, monkeypatch):
    """`ok=False` 要交出去（sched_job 据此 rc=1 并退避重试），不是吞掉。"""
    _fake_clock(bs, monkeypatch, 20, 0)   # 同上：不依赖真实钟点
    _stub_run_scan(monkeypatch, bs, ok=False)
    res = bs.run_daily_sync()
    assert res["ok"] is False
    assert res["error"] == "模拟失败"


# ---------------------------------------------------------------- 闸门共用
def test_gate_shared_by_both_entries(bs, monkeypatch):
    """`_daily_gate` 是唯一一份判断：两个入口在同样的场景下给出同样的结论。"""
    _fake_clock(bs, monkeypatch, 20, 0)   # 同上：不依赖真实钟点
    calls = _stub_run_scan(monkeypatch, bs)
    monkeypatch.setattr(bs, "_last_trade_date", lambda: "1999-01-01")
    assert bs._daily_gate() == "not_trading_day"
    with_skip = bs.run_daily_sync()
    assert with_skip["skipped"] == "not_trading_day"
    assert bs.maybe_daily() is False
    assert calls == [], "被闸挡住时不得真的开扫"

    # 当日已有结果 → done
    monkeypatch.setattr(bs, "_last_trade_date", lambda: TODAY)
    monkeypatch.setattr(bs, "_result_cache_load", lambda: {"status": "done", "date": TODAY})
    assert bs._daily_gate() == "done"
    assert bs.run_daily_sync()["skipped"] == "done"
    assert calls == []


def test_gate_pre_close_before_15(bs, monkeypatch):
    """15 点前不扫（历史行为的边界，别在重构里丢掉）。"""
    calls = _stub_run_scan(monkeypatch, bs)
    real_localtime = time.localtime
    monkeypatch.setattr(bs, "time", types.SimpleNamespace(
        sleep=lambda s: None, time=time.time, strftime=time.strftime,
        localtime=lambda *a: time.struct_time(
            (int(TODAY[0:4]), int(TODAY[5:7]), int(TODAY[8:10]), 9, 0, 0, 4, 303, 0))))
    assert bs._daily_gate() == "pre_close"
    monkeypatch.setattr(bs, "time", types.SimpleNamespace(
        sleep=lambda s: None, time=time.time, strftime=time.strftime,
        localtime=real_localtime))
    assert calls == []


# ---------------------------------------------------------------- 让路窗口（17:00–19:30）
def _fake_clock(bs, monkeypatch, hour: int, minute: int):
    """把 `bili_scan.time` 换成「固定在某时某分」的替身（其余方法沿用真 time）。"""
    _TY, _TM, _TD = (int(x) for x in TODAY.split("-"))
    # 2026-10-31 修：原写死 (2026,10,30)。`_daily_gate` 拿 `_last_trade_date()`（= TODAY，真今天）
    # 和伪时钟的 strftime 比，写死之后一过 10-30 就全部落到 not_trading_day ——
    # 6 个用例集体变红，而代码一行没动。日期必须从 TODAY 推。
    st = time.struct_time((_TY, _TM, _TD, hour, minute, 0, 4, 303, 0))
    monkeypatch.setattr(bs, "time", types.SimpleNamespace(
        sleep=lambda s: None, time=time.time, strftime=time.strftime,
        localtime=lambda *a: st))


def test_never_competes_with_lhb_at_1800(bs, monkeypatch):
    """18:00 **绝不开扫** —— 今晚就是靠这条保命。

    为什么是 P0：bili_scan 改同步后单轮 1575s（26 分钟），占的是刹车闸的 `FETCH 1/1`；
    `lhb_daily` 同类别、同写域，18:00 才发布、必须当晚落地。断言"没开扫"才等价于
    "没占闸" —— 只断言返回值的话，把 skip 写在 `_run_scan` 之后也能蒙过去。
    """
    monkeypatch.setattr(bs, "_DAILY_WINDOW", (19 * 60 + 45, 23 * 60))
    calls = _stub_run_scan(monkeypatch, bs)
    _fake_clock(bs, monkeypatch, 18, 0)
    assert bs._daily_gate() == "day_brake_window"
    res = bs.run_daily_sync()
    assert res["ok"] is True and res["skipped"] == "day_brake_window"
    assert bs.maybe_daily() is False
    assert calls == [], "18:00 起了扫描 = 占闸 1575s，龙虎榜当晚那一轮直接没有坑位"


def test_daily_window_edges(bs, monkeypatch):
    """闭区间 [19:45, 23:00]：19:44 skip / 19:45 放行 / 22:59 放行 / 23:00 放行 / 23:01 skip。

    白天另取 15:00、18:00 两个采样点 —— 整个白天都不许跑（理由见 `_DAILY_WINDOW`）。
    每条都断言"没开扫"，因为闸的价值在 `_run_scan` 有没有被调用，不在返回值。
    """
    monkeypatch.setattr(bs, "_DAILY_WINDOW", (19 * 60 + 45, 23 * 60))
    calls = _stub_run_scan(monkeypatch, bs)
    cases = ((15, 0, False), (18, 0, False), (19, 44, False),
             (19, 45, True), (22, 59, True), (23, 0, True), (23, 1, False))
    for hh, mm, want_run in cases:
        _fake_clock(bs, monkeypatch, hh, mm)
        got = bs._daily_gate()
        if want_run:
            assert got is None, "%02d:%02d 应放行，实际 %s" % (hh, mm, got)
        else:
            assert got == "day_brake_window", "%02d:%02d 应 skip，实际 %s" % (hh, mm, got)
    assert calls == []


def test_prewarm_still_runs_when_daily_was_missed(bs, monkeypatch):
    """**昨晚日更没跑成 → 早盘必须预热**，不能被执行窗口挡掉。

    这是 prewarm 唯一不可替代的价值：早盘 `_last_trade_date()` 有 15:05 截断、返回的是
    **上一个交易日**，缓存比它还旧就说明昨晚那一轮真没成。此时不预热，用户早上点
    「一键筛股」就要干等 26 分钟 warming。2026-10-30 我把窗口做成硬门，正好把这条兜底关掉了。
    """
    monkeypatch.setattr(bs, "_DAILY_WINDOW", (19 * 60 + 45, 23 * 60))
    calls = _stub_run_scan(monkeypatch, bs)
    monkeypatch.setattr(bs, "_result_cache_load", lambda: {"date": "2026-10-29"})
    _fake_clock(bs, monkeypatch, 9, 0)
    bs.maybe_prewarm()
    deadline = time.time() + 3.0
    while time.time() < deadline and (bs._THREAD is None or bs._THREAD.ident is None):
        time.sleep(0.02)
    assert bs._THREAD is not None and bs._THREAD.ident is not None, \
        "日更没跑成时早盘预热被窗口挡掉了（兜底没了）"
    bs._THREAD.join(timeout=3)
    assert calls == [False]


def test_prewarm_skips_when_cache_is_fresh(bs, monkeypatch):
    """缓存已是当日的 → 不预热。白天重启绝大多数走这条（15:05 截断让缓存自然匹配）。"""
    monkeypatch.setattr(bs, "_DAILY_WINDOW", (19 * 60 + 45, 23 * 60))
    calls = _stub_run_scan(monkeypatch, bs)
    monkeypatch.setattr(bs, "_result_cache_load", lambda: {"date": TODAY})
    _fake_clock(bs, monkeypatch, 9, 0)
    bs.maybe_prewarm()
    time.sleep(0.3)
    assert bs._THREAD is None and calls == []


def test_prewarm_does_not_grab_brake_before_daily_window(bs, monkeypatch):
    """15:05 之后、日更 19:45 之前的重启：缓存"不是当日"是**正常的**，别抢闸。

    这条正是"昨晚失败"和"今天的日更还没到点"的分界线：15:05 之后两者长得一模一样
    （`target` 已变成今天、缓存是昨天），但只有前者值得白天预热。
    """
    monkeypatch.setattr(bs, "_DAILY_WINDOW", (19 * 60 + 45, 23 * 60))
    calls = _stub_run_scan(monkeypatch, bs)
    monkeypatch.setattr(bs, "_result_cache_load", lambda: {"date": "2026-10-29"})
    _fake_clock(bs, monkeypatch, 16, 0)
    bs.maybe_prewarm()
    time.sleep(0.3)
    assert bs._THREAD is None, \
        "15:05 后的重启不该预热：日更 19:45 会补，这儿起扫只白抢 26 分钟闸"
    assert calls == []


def test_prewarm_runs_inside_daily_window(bs, monkeypatch):
    """日更时段（19:45–23:00）内缓存不新鲜 → 预热，与日更共用同一份窗口判断。"""
    monkeypatch.setattr(bs, "_DAILY_WINDOW", (19 * 60 + 45, 23 * 60))
    calls = _stub_run_scan(monkeypatch, bs)
    monkeypatch.setattr(bs, "_result_cache_load", lambda: {"date": "2026-10-29"})
    _fake_clock(bs, monkeypatch, 20, 30)
    bs.maybe_prewarm()
    deadline = time.time() + 3.0
    while time.time() < deadline and (bs._THREAD is None or bs._THREAD.ident is None):
        time.sleep(0.02)
    assert bs._THREAD is not None and bs._THREAD.ident is not None
    bs._THREAD.join(timeout=3)
    assert calls == [False]
