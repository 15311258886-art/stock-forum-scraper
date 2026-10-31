# -*- coding: utf-8 -*-
"""无限循环回归：`pillars_daily` / `predictor_daily` 必须是「每天最多跑一次」，不能结束即重生。

## bug 的形状（2026-10-30 现场抓到）

`viewer/logs/sched-pillars_daily.log`：
```
=== pillars_daily 2026-10-30 16:37:28 ===
[pillars_daily] ok 2327.5s {'generate': {'ok': True, 'run_date': '2026-10-30', ...}}   ← 成功
=== pillars_daily 2026-10-30 17:16:22 ===                                              ← 立刻又一轮
```

机理三件套（缺一不成）：

1. `_sched_tasks()` 里两者是**直连** `lambda: _spawn_offproc("pillars_daily")`，**没走 `_daily_job`**
   → 子进程成功退出后**没有任何"当日已完成"标记**写进 `meta`；
2. `_OFFPROC_GAP` 里两者都没登记 → 吃默认 **600s**；
3. 单轮实测耗时 `pillars_daily` **2327s**、`predictor_daily` 冷缓存跑满 **5400s** 被超时杀
   → 600 < 2327 → **上一轮一结束，间隔条件立刻满足 → 下一 tick 立刻重生**。

两者都是 `(COMPUTE, "app")`（`viewer/kernel/brake.py:79-80`），于是 `写域 app` 被连续占死，
`lhb_daily`（同样 app 域）永远抢不到闸 —— 10-30 整条日更链欠账就是这么来的。

子进程里的 `_GEN_DONE`（`pillars.py:130`，用在 1288/1290/1298~1303）与
`_DAILY_DONE`（`predictor.py:3885`，用在 4040~4064）都是**进程内字典**，而
`tools/sched_job.py` 每进程只调一次 `maybe_daily` → 它在子进程生命周期里**不可能被读到第二次**，
从 2026-10-15 出进程化那天起就是死代码。所以循环与它无关，它只是"看起来像个幂等闸"。

## 本文件测什么

1. **接线**：`_sched_tasks()` 里这两个任务必须路由到 `_daily_job`（且 deadline 与子进程同源）；
2. **断循环**：子进程成功退出（`_JOB_LAST_OK` 有今天的戳）→ 写 `meta.<job>_done` → **当轮与后续都不再 spawn**；
3. **不误杀**：没有成功戳、也没有标记时必须照常 spawn（否则就成了"永不运行"）；
4. **旧标记不吃**：`meta` 里是昨天的标记 → 今天照常 spawn；
5. **到点/周末/退避** 三道前置闸仍然生效。

`_META_CACHE` 的 10 秒缓存不构成障碍：它只影响"读标记"的频次，成功后写标记那一步是
`DB.set_meta` 直写 + 同步回填缓存（见 `_daily_job`），与任务跑了 39 分钟无关。
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _detach_prod_log_handlers() -> list:
    """摘掉根 logger 的 FileHandler（导入 `viewer.server` 会挂一个写生产日志的）。

    教训见 `tests/test_offproc_backoff_retry.py`：不摘的话，测试里的 `logging.*`
    会直接写进 `viewer/logs/server-<date>.log`。
    """
    root = logging.getLogger()
    removed = [h for h in list(root.handlers) if isinstance(h, logging.FileHandler)]
    for h in removed:
        root.removeHandler(h)
    return removed


from viewer import server as S  # noqa: E402

_DETACHED = _detach_prod_log_handlers()

# 2026-10-30 是周五（`_daily_job` 周末直接 return，必须用工作日做基准）
FRI = datetime(2026, 10, 30, 16, 30, 0)
SAT = datetime(2026, 10, 31, 16, 30, 0)
TODAY = "2026-10-30"
# 2026-10-31 修：上面三处原写 `_JOB_LAST_OK[JOB] = time.time()`（**真**时钟），
# 而 `_daily_job` 读它时用 `time.localtime(ok_at)` 还原日期、再和**伪**时钟的 day 比。
# 真时钟走过 2026-10-30 之后，两者必然不等 ⇒ 三个用例集体变红 —— 与代码无关，
# 是测试自身绑死了一天。改成从伪时钟取戳，任何一天跑都成立。
JOB = "daily_job_gate_probe"


class _FakeDB:
    """只实现 `_daily_job` 用到的两个方法，状态放内存 dict。"""

    def __init__(self):
        self.meta: dict = {}
        self.set_calls: list = []

    def set_meta(self, key, value):
        self.meta[key] = value
        self.set_calls.append((key, value))

    def get_meta(self, key):
        return self.meta.get(key)


def _fake_dt_class(moment: datetime):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return moment

    return _DT


@pytest.fixture()
def sv(monkeypatch):
    """隔离 `_daily_job` 的外部依赖：时钟、DB、meta 缓存、退避。"""
    saved_meta_cache = dict(S._META_CACHE)
    saved_last_ok = dict(S._JOB_LAST_OK)
    S._META_CACHE.clear()
    S._JOB_LAST_OK.clear()

    monkeypatch.setattr(S, "datetime", _fake_dt_class(FRI))
    fake_db = _FakeDB()
    monkeypatch.setattr(S, "DB", fake_db)
    monkeypatch.setattr(S, "BRAKE_backoff_wait", lambda job: 0.0)

    spawns: list[str] = []

    def fake_spawn(job, logname=None):  # noqa: ARG001
        spawns.append(job)
        return True

    monkeypatch.setattr(S, "_spawn_offproc", fake_spawn)
    yield S, fake_db, spawns

    S._META_CACHE.clear()
    S._META_CACHE.update(saved_meta_cache)
    S._JOB_LAST_OK.clear()
    S._JOB_LAST_OK.update(saved_last_ok)


# ---------------------------------------------------------------- 1. 接线
def test_scheduler_routes_both_jobs_through_daily_job(monkeypatch):
    """`_sched_tasks()` 里这两个任务必须走 `_daily_job`，且 deadline 与子进程闸同源。"""
    calls: list[tuple] = []
    monkeypatch.setattr(S, "_daily_job", lambda job, hm: calls.append((job, hm)))

    tasks = dict(S._sched_tasks())
    tasks["pillars_daily"]()
    tasks["predictor_daily"]()

    assert ("pillars_daily", (16, 0)) in calls, (
        "pillars_daily 没走 _daily_job → 成功退出后不写当日标记 → 600s 后立刻重生（无限循环）")
    assert ("predictor_daily", (15, 5)) in calls, (
        "predictor_daily 没走 _daily_job → 同上（它今天跑满 5400s 被超时杀）")
    # deadline 必须与各自 maybe_daily 内部的时间窗一致：
    #   pillars.maybe_daily 判 (16, 0)；predictor.maybe_daily 判 (15, 5)
    assert dict(calls)["pillars_daily"] == (16, 0)
    assert dict(calls)["predictor_daily"] == (15, 5)


def test_scheduler_no_longer_calls_spawn_offproc_directly(monkeypatch):
    """结构护栏：这两个任务的本轮入口不许再直接 `_spawn_offproc`。"""
    import inspect

    src = inspect.getsource(S._sched_tasks)
    assert '"_daily_job("pillars_daily"' not in src          # 防误改引用形式
    assert 'lambda: _spawn_offproc("pillars_daily")' not in src, (
        "pillars_daily 又变回直连 _spawn_offproc 了")
    assert 'lambda: _spawn_offproc("predictor_daily")' not in src, (
        "predictor_daily 又变回直连 _spawn_offproc 了")


# ---------------------------------------------------------------- 2. 断循环
def test_success_writes_done_flag_and_stops_respawning(sv):
    """核心：子进程成功退出 → 写 `meta.<job>_done` → 当轮不 spawn，后续也不 spawn。

    修复前（直连 `_spawn_offproc`）这条必红：没有任何代码会在成功后写标记。
    """
    s, fake_db, spawns = sv
    flag = JOB + "_done"

    # 上一轮已成功退出（`_spawn_offproc` 成功分支写的就是这个）
    s._JOB_LAST_OK[JOB] = FRI.timestamp()

    s._daily_job(JOB, (16, 0))
    assert spawns == [], "已经成功过的一天不该再 spawn"
    assert fake_db.meta.get(flag) == TODAY, "成功退出后必须补写当日完成标记（这是断循环的那一步）"

    # 后续 tick：命中标记直接返回
    for _ in range(5):
        s._daily_job(JOB, (16, 0))
    assert spawns == [], "写了标记之后仍然 spawn → 循环没断"
    assert len(fake_db.set_calls) == 1, "标记只该写一次，不该每 tick 重写"


def test_flag_written_by_success_is_read_back_from_meta(sv, monkeypatch):
    """标记落库后，后续 tick 从 `meta` 读到（而不是靠 `_JOB_LAST_OK` 这个内存变量）。

    意义：父进程重启会清空 `_JOB_LAST_OK`，但 `meta` 里的标记必须还在 —— 否则重启即重生。
    """
    s, fake_db, spawns = sv
    s._JOB_LAST_OK[JOB] = FRI.timestamp()
    s._daily_job(JOB, (16, 0))
    assert fake_db.meta.get(JOB + "_done") == TODAY

    # 模拟父进程重启：内存里的成功戳没了，标记还在
    s._JOB_LAST_OK.clear()
    s._META_CACHE.clear()
    s._daily_job(JOB, (16, 0))
    assert spawns == [], "重启后仅凭 meta 标记也必须判为『今天做过了』"


# ---------------------------------------------------------------- 3. 不误杀
def test_spawns_when_nothing_done_today(sv):
    """没有成功戳、也没有标记 → 必须照常 spawn（否则就变成『永不运行』）。"""
    s, fake_db, spawns = sv
    s._daily_job(JOB, (16, 0))
    assert spawns == [JOB]
    assert fake_db.set_calls == [], "spawn 成功不等于成功跑完，绝不能提前写标记"


def test_stale_flag_from_yesterday_does_not_block_today(sv):
    """`meta` 里是昨天的标记 → 今天照常 spawn。"""
    s, fake_db, spawns = sv
    fake_db.meta[JOB + "_done"] = "2026-10-29"
    s._daily_job(JOB, (16, 0))
    assert spawns == [JOB]


# ---------------------------------------------------------------- 4. 三道前置闸
def test_does_not_spawn_before_deadline(sv, monkeypatch):
    """未到 deadline（16:00 前）不 spawn —— 与子进程 `maybe_daily` 的 (16,0) 同源。"""
    s, _fake_db, spawns = sv
    monkeypatch.setattr(s, "datetime", _fake_dt_class(datetime(2026, 10, 30, 15, 59, 0)))
    s._daily_job(JOB, (16, 0))
    assert spawns == []


def test_does_not_spawn_on_weekend(sv, monkeypatch):
    s, _fake_db, spawns = sv
    monkeypatch.setattr(s, "datetime", _fake_dt_class(SAT))
    s._daily_job(JOB, (16, 0))
    assert spawns == []


def test_backoff_window_blocks_spawn(sv, monkeypatch):
    """失败退避窗口内不 spawn（这条与 `_JOB_PROCS.pop` 修复配套：窗口过期后仍要能重试）。"""
    s, _fake_db, spawns = sv
    monkeypatch.setattr(s, "BRAKE_backoff_wait", lambda job: 900.0)
    s._daily_job(JOB, (16, 0))
    assert spawns == []


# ---------------------------------------------------------------- 5. 与修复 1 的联动
def test_success_after_retry_still_writes_flag(sv):
    """失败重试 → 终于成功 → 仍要写标记并停手（`_JOB_PROCS.pop` 保证重试可达）。"""
    s, fake_db, spawns = sv
    s._daily_job(JOB, (16, 0))                     # 第 1 次 spawn（会失败，无标记）
    assert spawns == [JOB]
    assert fake_db.meta.get(JOB + "_done") is None

    s._JOB_LAST_OK[JOB] = FRI.timestamp()              # 第 2 轮成功退出
    s._daily_job(JOB, (16, 0))
    assert spawns == [JOB], "成功之后不许再 spawn"
    assert fake_db.meta.get(JOB + "_done") == TODAY
