# -*- coding: utf-8 -*-
"""D33 回归：`lhb_daily` 在**短命子进程**里必须把一轮真正跑完，不能"起了 daemon 线程就 return"。

## 这个 bug 的形状（2026-10-30 定位，骗了 11 天）

```
lhb_daily.maybe_daily()  →  threading.Thread(target=_worker, daemon=True).start()
                         →  return {"started": today}      ← 立刻返回
tools/sched_job.py       →  拿到 dict → print "ok 0.1s" → return 0
                         →  进程退出
CPython 关解释器          →  **直接杀掉 daemon 线程** → _worker() 一行没跑
                         →  record_result() 永不执行 → last_date 停在旧值 → 第二天重演
```

后果实测：`meta.lhb_daily_state.last_run_at` 冻结在 `2026-10-19 22:24:56`，
`viewer/logs/lhb_daily.log` 最后一条也是 10-19；此后 **191 次** `{'started': today}`、每次
**0.1~0.2s**（真跑一轮约 484s）。于是 10-20 起龙虎榜/板块扩散/主线研判全部停更，
10-29 的 `lhb_seats/lhb_depts/lhb_inst` 三表全 0 只是它露出水面的那一角。

**注意「返回码 0」在这条链上没有信息量**：0.1s 的 `{'started'}` 和 484s 的真跑都是 rc=0。
所以本文件测的不是返回码，而是「函数返回那一刻，活是否真的干完了」。

## 为什么不能用 `python lhb_daily.py --force` 当验收证据

`main()`（`lhb_daily.py` 末尾）走的是 `run_once()` **在主进程里同步跑**，它压根不起线程，
这条路径从来没坏过。它只能证明「三个子脚本还活着」，证明不了 offproc 路径修好了。
同理，18:00 之前走真路径 `sched_job.py lhb_daily` 会被 `skip: pre_publish` 挡住、什么也验不出
—— 所以这里用**桩 `run_once` + 桩时间闸**，不依赖日历、不联网、不跑真抓取。

全部跑在临时 sqlite 上，**不碰生产库**（`lhb_daily.DB` 被重定向到 `tmp_path`）。
"""
from __future__ import annotations

import importlib
import inspect
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TODAY = datetime.now().strftime("%Y-%m-%d")
SENTINEL_KEY = "offproc_sentinel"


@pytest.fixture()
def ld(monkeypatch, tmp_path):
    """lhb_daily 指向临时库；时间闸强制放行；日志不落盘；running 标志复位。

    收尾那几行是**事故防线**，不是洁癖（2026-10-30 15:31:05 真炸过一次）：
    被测的旧实现是「起 daemon 线程就 return」，线程还要跑 0.3s。如果测试结束就撤销
    monkeypatch，那个孤儿线程随后会拿着**生产** `DB`/`LOG_FILE` 把桩数据写进生产库 ——
    实测后果是 `viewer/logs/lhb_daily.log` 多 4 条假行、`meta.last_date` 被推成当天，
    当晚 18:00 判 `skip: done_today`，真正该跑的那一轮被自己的测试挡掉。
    所以 join 必须排在 `yield` 之后、monkeypatch 撤销**之前**（此时线程看到的还是 tmp 路径）。
    `LOG_FILE` 那行是纵深：join 只管本进程的线程，万一将来换了 harness，日志这半边还有兜底。
    """
    mod = importlib.import_module("lhb_daily")
    monkeypatch.setattr(mod, "DB", str(tmp_path / "app.db"))
    monkeypatch.setattr(mod, "LOG_FILE", str(tmp_path / "lhb_daily.log"))
    monkeypatch.setattr(mod, "_append_result_log", lambda res: None)
    monkeypatch.setattr(mod, "_is_weekday", lambda now: True)
    monkeypatch.setattr(mod, "_past_publish", lambda now: True)
    mod._running["v"] = False
    yield mod
    for t in threading.enumerate():
        if t.name in ("lhb-daily", "blogger-daily"):
            t.join(timeout=10)
    assert not [t for t in threading.enumerate() if t.name == "lhb-daily"], \
        "测试遗留了 lhb-daily 线程：它会在 monkeypatch 撤销后写生产库/生产日志"
    mod._running["v"] = False


def _conn(ld) -> sqlite3.Connection:
    con = sqlite3.connect(ld.DB)
    ld.ensure_schema(con)
    return con


def _state(ld) -> dict:
    con = _conn(ld)
    try:
        return ld._state_read(con)
    finally:
        con.close()


def _sentinel(ld):
    """独立连接读哨兵：证明是**落盘**的，不是某个对象里的内存残留。"""
    con = _conn(ld)
    try:
        return ld._meta_get(con, SENTINEL_KEY)
    finally:
        con.close()


def _stub_run_once(ld, monkeypatch, seconds: float = 0.3, ok: bool = True):
    """把 `run_once` 换成「睡一会儿 + 往 meta 写哨兵」的桩，返回调用记录。

    哨兵是关键：它在 `run_once` **体内**写入。若调用方是"起线程就 return"，
    函数返回时哨兵一定还不存在 —— 这条断言就是本 bug 的判据。
    """
    calls: list[float] = []

    def fake_run_once(*args, **kwargs):  # noqa: ARG001
        time.sleep(seconds)
        calls.append(time.time())
        con = _conn(ld)
        try:
            ld._meta_set(con, SENTINEL_KEY, "done")
        finally:
            con.close()
        return {"ok": ok, "started": "2026-10-30 18:00:00", "ended": "2026-10-30 18:08:04",
                "steps": [{"script": "lhb_fetch.py", "returncode": 0 if ok else 1,
                           "seconds": 484.0}]}

    monkeypatch.setattr(ld, "run_once", fake_run_once)
    return calls


# ---------------------------------------------------------------- 1. 核心判据
def test_run_daily_sync_finishes_before_returning(ld, monkeypatch):
    """`run_daily_sync()` **返回那一刻**：桩已执行完、哨兵已落盘、状态已回写。

    旧实现（起 daemon 线程后立即 return）在这条上必红：返回时哨兵不存在、last_run_at 不前进。
    """
    calls = _stub_run_once(ld, monkeypatch)
    before = _state(ld)
    assert before.get("last_run_at") is None, "初始状态应为空（否则断言不成立）"

    t0 = time.time()
    with _conn(ld) as con:
        res = ld.run_daily_sync(con)
    elapsed = time.time() - t0

    assert calls, "run_once 从未被调用 —— 这一轮根本没跑"
    assert _sentinel(ld) == "done", (
        "函数已返回但 run_once 体内的哨兵不存在 → 又是「先 return、后台慢慢跑」")
    assert elapsed >= 0.3, "返回得太快，说明没有等 run_once 跑完：%.3fs" % elapsed

    assert res.get("started") == TODAY
    assert res.get("ok") is True
    assert res.get("steps") and res["steps"][0]["script"] == "lhb_fetch.py"

    st = _state(ld)
    assert st.get("last_run_at"), "record_result 没执行（last_run_at 未写）"
    assert st.get("last_ok_at"), "record_result 没执行（last_ok_at 未写）"
    assert st.get("last_date") == TODAY, "成功轮必须写 last_date，否则当天会重复跑"
    assert int(st.get("tries", 0)) == 0
    assert st.get("last_trigger_at"), "last_trigger_at 应记录（供巡检）"


def test_maybe_daily_is_synchronous_too(ld, monkeypatch):
    """`maybe_daily()` 是 sched_job 历史上按名调用的入口，行为也必须同步。

    （`tools/sched_job.py` 已改为显式调 `run_daily_sync`，但巡检脚本/人们仍可能按旧名调。）
    """
    calls = _stub_run_once(ld, monkeypatch)
    with _conn(ld) as con:
        res = ld.maybe_daily(con)
    assert calls and _sentinel(ld) == "done", "maybe_daily 仍是「起线程就返回」"
    assert res.get("started") == TODAY
    assert _state(ld).get("last_date") == TODAY


def test_no_daemon_thread_left_in_scheduling_path(ld):
    """结构护栏：两个调度入口里不允许再出现 `Thread(...)`。

    返回值正确不代表实现正确 —— 真正把人坑了 11 天的是"返回得对、活没干"。
    """
    for fn in (ld.run_daily_sync, ld.maybe_daily):
        src = inspect.getsource(fn)
        assert "Thread(" not in src, "%s 里又起了线程，短命子进程里会被腰斩" % fn.__name__


# ---------------------------------------------------------------- 2. 失败要能被父进程看见
def test_failed_round_writes_tries_and_keeps_last_date(ld, monkeypatch):
    """失败轮：返回 `ok=False`（让 sched_job 判 rc=1）+ `tries=1` + **不动** `last_date`。"""
    _stub_run_once(ld, monkeypatch, ok=False)
    with _conn(ld) as con:
        res = ld.run_daily_sync(con)
    assert res.get("ok") is False, "失败轮必须把 ok=False 交出去，否则父进程记成「今天跑过了」"
    st = _state(ld)
    assert st.get("last_date") != TODAY
    assert int(st.get("tries")) == 1
    assert st.get("tries_date") == TODAY


def test_retry_after_failure_still_allowed_until_max_tries(ld, monkeypatch):
    """失败后仍要能重试，允许 MAX_TRIES(=3) 次；第 4 次才 failed_today（次日恢复）。"""
    _stub_run_once(ld, monkeypatch, seconds=0.0, ok=False)
    for i in (1, 2, 3):
        with _conn(ld) as con:
            res = ld.run_daily_sync(con)
        assert res.get("started") == TODAY, "第 %d 次失败后应仍允许重试：%s" % (i, res)
        assert int(_state(ld).get("tries")) == i
    with _conn(ld) as con:
        assert ld.run_daily_sync(con) == {"skip": "failed_today"}, (
            "MAX_TRIES 用完必须停手（防限流打爆数据源）")
    assert _state(ld).get("last_date") != TODAY, "三次都失败，绝不能记成今天已完成"


# ---------------------------------------------------------------- 3. 闸门共用 + 跳过不跑
def test_gate_is_shared_by_both_entries(ld, monkeypatch, tmp_path):
    """时间闸/去重闸必须由 `_gate` 一处提供，两个入口返回完全相同的 skip。"""
    calls = _stub_run_once(ld, monkeypatch, seconds=0.0)

    # 未到发布时点
    monkeypatch.setattr(ld, "_past_publish", lambda now: False)
    with _conn(ld) as con:
        a = ld.run_daily_sync(con)
    with _conn(ld) as con:
        b = ld.maybe_daily(con)
    assert a == b == {"skip": "pre_publish"}
    assert calls == [], "被闸挡住时不得调用 run_once"

    # 今天已成功过
    monkeypatch.setattr(ld, "_past_publish", lambda now: True)
    con = _conn(ld)
    ld._meta_set(con, ld.STATE_KEY, '{"last_date": "%s"}' % TODAY)
    con.close()
    with _conn(ld) as con:
        a = ld.run_daily_sync(con)
    with _conn(ld) as con:
        b = ld.maybe_daily(con)
    assert a == b == {"skip": "done_today"}
    assert calls == []


def test_pre_publish_does_not_touch_state(ld, monkeypatch):
    """18:00 前被挡住时，不应留下任何"跑过"的痕迹（否则 18:00 后会被自己的闸挡掉）。"""
    _stub_run_once(ld, monkeypatch, seconds=0.0)
    monkeypatch.setattr(ld, "_past_publish", lambda now: False)
    with _conn(ld) as con:
        ld.run_daily_sync(con)
    st = _state(ld)
    assert st.get("last_run_at") is None
    assert st.get("last_date") is None


# ================================================================
# 8. 进程级证明（2026-10-30 D33 事故后的加固）
#
# 为什么要有这一组：上一轮我用「在 pytest 进程内把 lhb_daily.py 换回旧版」来取红态证据，
# 旧版起的 daemon 线程**活过了 monkeypatch 撤销**，撤销后它看到的 DB/LOG_FILE 又是生产路径
# → 把桩数据写进了生产（last_date 被推成今天 → 当晚 18:00 被判 done_today → 真抓取不跑）。
# 教训：**红态证明必须在子进程里跑，且一开始就把路径指到 tmp**。
# 这里把「短命子进程」这件事本身当成被测对象：子进程 print 完立刻退出，不 join、不 sleep。
# ================================================================
import os          # noqa: E402
import subprocess  # noqa: E402
import textwrap    # noqa: E402

CHILD = textwrap.dedent(r'''
    import os, sqlite3, sys, time
    root, dbdir = sys.argv[1], sys.argv[2]
    sys.path[:0] = [root, os.path.join(root, "viewer")]
    impl = os.environ.get("LHB_IMPL") or ""
    if impl:
        import importlib.util
        spec = importlib.util.spec_from_file_location("lhb_impl_under_test", impl)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    else:
        import lhb_daily as m
    db = os.path.join(dbdir, "app.db")
    m.DB = db
    m.LOG_FILE = os.path.join(dbdir, "lhb_daily.log")
    m._is_weekday = lambda now: True
    m._past_publish = lambda now: True

    def fake_run_once(*a, **k):
        time.sleep(0.4)                      # 假装在跑三个子脚本
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE IF NOT EXISTS sentinel(k TEXT)")
        con.execute("INSERT INTO sentinel VALUES('ran')")
        con.commit(); con.close()
        return {"ok": True, "started": "s", "ended": "e",
                "steps": [{"script": "lhb_fetch.py", "returncode": 0, "seconds": 0.4}]}

    m.run_once = fake_run_once
    con = sqlite3.connect(db)
    m.ensure_schema(con)
    entry = getattr(m, "run_daily_sync", None) or m.maybe_daily
    print("RET", entry(con), flush=True)
    # ⚠️ 这里**立刻退出**：不 join、不 sleep。同步实现下活已干完；异步实现下线程会被杀。
''')

# 一个「旧形态」的最小实现，用来证明本文件的断言**真的能抓到那个 bug**。
# 它不依赖 git 历史，任何机器上都能复现红态，且只写 tmp_path。
OLD_IMPL = textwrap.dedent('''
    """旧形态（异步）：起 daemon 线程后立即返回 —— 短命子进程里必被腰斩。"""
    import os, sqlite3, threading, time
    from datetime import datetime

    BASE = os.path.dirname(os.path.abspath(__file__))
    DB = os.path.join(BASE, "nope.db")
    LOG_FILE = os.path.join(BASE, "nope.log")
    _running = {"v": False}

    def ensure_schema(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()

    def _worker(db_path):
        time.sleep(0.4)
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE IF NOT EXISTS sentinel(k TEXT)")
        con.execute("INSERT INTO sentinel VALUES('ran')")
        con.commit(); con.close()

    def maybe_daily(conn):
        threading.Thread(target=_worker, args=(DB,), daemon=True).start()
        return {"started": "today"}
''')


def _run_child(tmp_path: Path, impl: Path | None = None):
    env = dict(os.environ)
    if impl is not None:
        env["LHB_IMPL"] = str(impl)
    return subprocess.run([sys.executable, "-c", CHILD, str(ROOT), str(tmp_path)],
                          capture_output=True, text=True, timeout=180, env=env)


def _sentinel_in(tmp_path: Path):
    con = sqlite3.connect(tmp_path / "app.db")
    try:
        return con.execute("SELECT k FROM sentinel").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def test_child_process_finishes_work_before_exiting(tmp_path):
    """真·验收：子进程 print 完就退出，哨兵必须已经在（tmp 库）里。"""
    r = _run_child(tmp_path)
    assert r.returncode == 0, "子进程异常：%s\n%s" % (r.stdout, r.stderr)
    assert "RET" in r.stdout, r.stdout
    assert _sentinel_in(tmp_path) == [("ran",)], (
        "子进程退出后哨兵不存在 → 活没干完就走了（daemon 线程被杀），stdout=%s" % r.stdout)


def test_harness_would_catch_the_old_async_pattern(tmp_path):
    """反向验证：把「旧形态」实现喂给同一个子进程，哨兵必须**不存在**。

    这条是自证——证明上面那条断言不是"怎么写都绿"。它只用 tmp_path 里的合成模块，
    不读 git、不碰生产，也不起任何会活过子进程的东西（子进程自己就退了）。
    """
    impl = tmp_path / "old_shape.py"
    impl.write_text(OLD_IMPL, encoding="utf-8")
    r = _run_child(tmp_path, impl=impl)
    assert r.returncode == 0, r.stderr
    assert "RET {'started': 'today'}" in r.stdout, r.stdout
    assert _sentinel_in(tmp_path) == [], (
        "旧形态竟然把活干完了？说明这条检测手段失效（或机器慢到 0.4s 内跑完了），"
        "stdout=%s" % r.stdout)
