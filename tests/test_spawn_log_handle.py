# -*- coding: utf-8 -*-
"""`_spawn_offproc` 的日志句柄泄漏回归（2026-11-01，P1-5 顺带修）。

## 被测的 bug

```python
fh = open(os.path.join(log_dir, "sched-%s.log" % job), "a", ...)
proc = subprocess.Popen([...], stdout=fh, stderr=fh, ...)
# ← 从此再没有 fh.close()，函数返回后这个句柄既不在局部变量里、也不会被回收
```

CPython 靠引用计数回收 —— 函数返回时 `fh` 是局部变量，理论上会掉到 0 引用而被关掉。
**但 Popen 会把 `fh` 存进 `proc.stdout`**（这里是 `stdout=fh` 直接传文件对象，
`Popen` 内部 `self.stdout = stdout`），于是只要 `_JOB_PROCS[job]` 还握着 `proc`，
这个文件对象就**一直有强引用**，句柄就不会释放。

`_JOB_PROCS` 里的记录要等到收尾才摘，而收尾又依赖子进程退出 —— 也就是说
**每一个还在跑的后台任务都会一直占着一份日志句柄**，并且每一轮新起的进程再叠一份
（旧 proc 被 `_JOB_PROCS` 覆盖时才会掉引用 → 释放时机完全不可控）。

Windows 上句柄不是"内存吃紧会自动回收"的东西：涨到上限后表现是
"打开文件失败 / 进程起不来"，而报错点离真正原因非常远（这是最贵的一类 bug：
现象在 A、原因在 B）。所以这里显式 `finally: fh.close()`。

## 判据

1. `Popen` 收到的那个文件对象，**`_spawn_offproc` 返回时必须是 closed**；
2. 起 3 轮 → 开 3 个、关 3 个（数量相等，不靠"大概没涨"这种软判据）；
3. **异常路径也要关**（`Popen` 抛异常时原来同样在漏，而那正是故障期最频繁的路径）。

判据 2 的做法：只在测试期间包一层 `builtins.open`，**只记录文件名含 `sched-` 的句柄**。
不记录全部 open，是为了不被 pytest / logging 的其它文件操作干扰 —— 只测我关心的那一个。
"""
from __future__ import annotations

import builtins
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _detach_prod_log_handlers() -> list:
    """摘掉根 logger 上的 FileHandler —— 否则本文件的 `logging.info` 会写进生产日志。

    与 `tests/test_offproc_backoff_retry.py` 同样的理由（那里有详细现场记录）：
    `viewer/server.py` 导入时会往根 logger 挂 `viewer/logs/server-<date>.log`。
    """
    root = logging.getLogger()
    removed = [h for h in list(root.handlers) if isinstance(h, logging.FileHandler)]
    for h in removed:
        root.removeHandler(h)
    return removed


from viewer import server as S  # noqa: E402

_DETACHED = _detach_prod_log_handlers()

JOB = "fh_leak_probe"


class _FakeProc:
    """假 Popen：不真起进程。`stdout` 保存传入的句柄，供断言是否被关闭。"""

    def __init__(self, stdout=None):
        self.stdout = stdout
        self.stderr = stdout
        self.returncode = None
        self.pid = 515151

    def poll(self):
        # 恒为 None = "子进程还活着"：这样 `_spawn_offproc` 每次都会走到 Popen，
        # 也不会触发 `_enforce_job_timeout`（不 terminate，便于重复起 3 轮）。
        return None

    def terminate(self):
        pass

    def kill(self):
        pass


class _FakeLease:
    kind = "compute"
    domain = "app"


class _FakeBrake:
    """只需要"永远放行"：本文件测的是句柄，不是闸。"""

    def acquire(self, job):  # noqa: ARG002
        return _FakeLease()

    def release(self, job):  # noqa: ARG002
        pass

    def bind_pid(self, lease, pid):  # noqa: ARG002
        pass

    def mark_settled(self, job):  # noqa: ARG002
        pass

    def heartbeat(self, job, pid=None):  # noqa: ARG002
        return True

    def should_log(self, job, gap: float = 120.0):  # noqa: ARG002
        return False

    def why(self, job):  # noqa: ARG002
        return "fake"


@pytest.fixture()
def sv(monkeypatch, tmp_path):
    """清干净调度状态，把 `ROOT` 指到 tmp（`sched-*.log` 不落进 `viewer/logs`）。"""
    saved = {name: dict(getattr(S, name))
             for name in ("_JOB_PROCS", "_JOB_LAST", "_JOB_LAST_OK", "_JOB_SETTLED")}
    saved_warned = set(S._JOB_WARNED)
    saved_kill = set(S._JOB_KILL_PENDING)

    monkeypatch.setattr(S, "ROOT", str(tmp_path))
    S._JOB_PROCS.pop(JOB, None)
    S._JOB_LAST.pop(JOB, None)
    S._JOB_LAST_OK.pop(JOB, None)
    S._JOB_SETTLED.pop(JOB, None)
    S._JOB_WARNED.discard(JOB)
    S._JOB_KILL_PENDING.discard(JOB)

    monkeypatch.setattr(S, "BRAKE", _FakeBrake())
    # `_OFFPROC_GAP.get(job, 600)`：显式给个 0，才能连续起 3 轮（默认 600s 冷却会挡住第 2 轮）
    monkeypatch.setattr(S, "_OFFPROC_GAP", {JOB: 0})
    monkeypatch.setattr(S, "BRAKE_backoff_wait", lambda job: 0.0)   # noqa: ARG005
    monkeypatch.setattr(S, "BRAKE_backoff_ok", lambda job: None)    # noqa: ARG005
    monkeypatch.setattr(S, "BRAKE_backoff_fail", lambda job, err="": 60.0)  # noqa: ARG005
    # supervisor 线程与本文件无关（子进程恒不退出），关掉以免线程残留
    monkeypatch.setattr(S, "_job_supervisor", lambda *a, **k: None)

    yield S

    for name, value in saved.items():
        getattr(S, name).clear()
        getattr(S, name).update(value)
    S._JOB_WARNED.clear()
    S._JOB_WARNED.update(saved_warned)
    S._JOB_KILL_PENDING.clear()
    S._JOB_KILL_PENDING.update(saved_kill)


def _record_sched_opens(monkeypatch) -> list:
    """包一层 `builtins.open`，只记录文件名含 `sched-` 的句柄。返回记录列表。"""
    seen: list = []
    real_open = builtins.open

    def spy(file, *a, **kw):
        fh = real_open(file, *a, **kw)
        try:
            name = str(file)
        except Exception:  # noqa: BLE001
            name = ""
        if "sched-" in name and "log" in name:
            seen.append(fh)
        return fh

    monkeypatch.setattr(builtins, "open", spy)
    return seen


def test_log_handle_is_closed_after_spawn(sv, monkeypatch):
    """最小判据：Popen 收到的那份句柄，`_spawn_offproc` 返回时必须已关闭。"""
    captured: list = []

    def fake_popen(argv, **kwargs):  # noqa: ARG001
        proc = _FakeProc(stdout=kwargs.get("stdout"))
        captured.append(proc)
        return proc

    monkeypatch.setattr(sv.subprocess, "Popen", fake_popen)
    assert sv._spawn_offproc(JOB) is True
    assert len(captured) == 1
    assert captured[0].stdout is not None, "Popen 没拿到日志句柄，这个测试就没测到东西"
    assert captured[0].stdout.closed is True, (
        "父进程那份 sched-*.log 句柄没有关闭 —— 每起一个子进程泄漏一个句柄")


def test_open_count_equals_close_count_over_three_spawns(sv, monkeypatch):
    """连起 3 轮：开 3 个 `sched-*` 句柄，必须关 3 个（数量相等，不是"看起来没涨"）。"""
    procs: list = []

    def fake_popen(argv, **kwargs):  # noqa: ARG001
        proc = _FakeProc(stdout=kwargs.get("stdout"))
        procs.append(proc)
        return proc

    monkeypatch.setattr(sv.subprocess, "Popen", fake_popen)
    handles = _record_sched_opens(monkeypatch)

    for _ in range(3):
        # 每轮先把上一轮记录摘掉，模拟"子进程已结束、收尾已做"的稳态
        sv._JOB_PROCS.pop(JOB, None)
        assert sv._spawn_offproc(JOB) is True

    assert len(handles) == 3, "应恰好打开 3 个 sched 日志句柄，实际 %d" % len(handles)
    still_open = [h for h in handles if not h.closed]
    assert not still_open, "有 %d 个日志句柄没关（open=%d / close=%d）" % (
        len(still_open), len(handles), len(handles) - len(still_open))
    assert len(procs) == 3


def test_log_handle_is_closed_even_when_popen_raises(sv, monkeypatch):
    """异常路径：`Popen` 抛异常时递过去的句柄同样必须关闭。

    这条是"顺手一起修"的证明 —— 旧实现在这条路也在漏，而"子进程起不来"的故障期
    恰恰是进程最脆弱的时候，最不该再叠一层句柄泄漏。
    """
    def boom(argv, **kwargs):  # noqa: ARG001
        boom.handle = kwargs.get("stdout")
        raise OSError("模拟 CreateProcess 失败")

    monkeypatch.setattr(sv.subprocess, "Popen", boom)
    handles = _record_sched_opens(monkeypatch)

    assert sv._spawn_offproc(JOB) is False, "起不来应返回 False（本轮跳过）"
    assert len(handles) == 1
    assert handles[0] is boom.handle, "Popen 拿到的不是同一个句柄，断言无效"
    assert handles[0].closed is True, "Popen 抛异常时日志句柄仍然泄漏"
