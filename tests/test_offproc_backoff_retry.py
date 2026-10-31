# -*- coding: utf-8 -*-
"""调度回归：子进程**一次**非零退出后，退避窗口过期必须能再次启动。

## 被测的 bug（2026-10-30 定位）

`viewer/server.py::_spawn_offproc` 的失败分支只做了 `BRAKE_backoff_fail` 就 `return False`，
**从不 `_JOB_PROCS.pop(job)`**。于是那条死记录永远留在表里、`prev.returncode` 恒为真 →
此后**每个 tick（10 秒一次）**都重新进同一个分支：

  · 反复 `BRAKE_backoff_fail(job, ...)`，把退避窗口一次次往后推（60→300→900→1800 封顶）；
  · 立刻 `return False`，因此永远走不到函数后段 `wait = BRAKE_backoff_wait(job)` 的门控
    → **退避窗口即使过期也没有任何路径能重试**，`MAX_TRIES=3` 形同虚设。

`_daily_job` 包着的任务有 `if BRAKE_backoff_wait(job) > 0: return` 提前 return 这层保护，
不至于反复 bump；但 `_sched_tasks()` 里 `("lhb_daily", lambda: _spawn_offproc("lhb_daily"))`
是**直连**（`predictor_daily` / `pillars_daily` / `bili_scan` / `resonance_build` / `selfpick_build`
/ `evolve_daily` 同理），没有这层保护 —— 一次非零退出就等于**直到服务重启都不再启动**。

## 本文件的四条判据

1. 退避窗口**过期后能再次 Popen**（核心，旧实现必红）；
2. 退避窗口**内不启动**（修完以后仍然要遵守退避，不能变成"失败就每 tick 重开"）；
3. 一次失败只记**一次** backoff（旧实现随 tick 次数线性增长，这条专治"窗口被无限后推"）；
4. 成功分支**必须保留** `_JOB_PROCS` 记录 —— `_daily_job` 靠 `_JOB_LAST_OK` 补写
   "当日已完成"标记，把成功侧的账打断会引入新问题。所以修复是**失败侧单边 pop**。

全部在**桩对象**上跑：不真起进程、不联网、不碰闸单例、不写 `viewer/logs`（`ROOT` 指到 tmp）。
"""
from __future__ import annotations

import subprocess  # noqa: F401  —— 用于确认 DEVNULL 等常量仍可用
import logging
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _detach_prod_log_handlers() -> list:
    """摘掉根 logger 上的 FileHandler。

    ⚠️ 为什么必须做（2026-10-30 现场教训）：`viewer/server.py` 在**导入时**调
    `_setup_logging()`，往根 logger 挂一个 `RotatingFileHandler`，指向
    `viewer/logs/server-<YYYYMMDD>.log`。于是这个 pytest 进程里任何 `logging.info(...)`
    都会写进**生产日志** —— 实测本文件第一版跑下来，生产日志 17:20~17:22 多出 6 行
    `[brake] offproc_retry_probe 放行（pid=424242…）`。虽然只是日志、不动任何状态，
    但会污染事后排查（"这个 job 是谁放的？"）。
    """
    root = logging.getLogger()
    removed = [h for h in list(root.handlers) if isinstance(h, logging.FileHandler)]
    for h in removed:
        root.removeHandler(h)
    return removed


# 导入即摘：本文件所有用例都在桩对象上跑，不需要也不允许写生产日志。
from viewer import server as S  # noqa: E402

_DETACHED = _detach_prod_log_handlers()

# 任一未登记任务名即可（`_OFFPROC_GAP` 用默认 600s，与 lhb_daily 对齐）
JOB = "offproc_retry_probe"


class _FakeProc:
    """假 Popen 对象：`poll()` 恒定返回预设退出码，`pid` 固定。"""

    def __init__(self, returncode: int):
        self.returncode = returncode
        self.pid = 424242
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1


class _FakeLease:
    kind = "compute"
    domain = "app"


class _FakeBrake:
    """替掉全局 `BRAKE` 单例：只记录 acquire/release/bind_pid 次数。

    不复用真单例有两个原因：① 单例是进程级共享状态，测试之间会互相污染；
    ② `acquire` 受 `_MAX_TOTAL` 总闸影响，别的测试残留租约会让本文件"莫名其妙红"。
    """

    def __init__(self):
        self.acquired = 0
        self.released = 0
        self.pids: list[int] = []

    def acquire(self, job):  # noqa: ARG002
        self.acquired += 1
        return _FakeLease()

    def release(self, job):  # noqa: ARG002
        self.released += 1

    def bind_pid(self, lease, pid):  # noqa: ARG002
        self.pids.append(int(pid))

    # 2026-11-01：`Brake` 接口新增的两个成员 —— 收尾路径（`_job_settle`）会调
    # `mark_settled`，supervisor 线程会调 `heartbeat`。桩对象必须补齐，
    # 否则收尾会抛 AttributeError，本文件会以"与退避无关"的方式全红。
    def mark_settled(self, job):  # noqa: ARG002
        self.settled = getattr(self, "settled", 0) + 1

    def heartbeat(self, job, pid=None):  # noqa: ARG002
        return True

    def should_log(self, job, gap: float = 120.0):  # noqa: ARG002
        return True

    def why(self, job):  # noqa: ARG002
        return "fake"


def _clear_state(S) -> None:
    S._JOB_PROCS.pop(JOB, None)
    S._JOB_LAST.pop(JOB, None)
    S._JOB_LAST_OK.pop(JOB, None)
    S._JOB_WARNED.discard(JOB)
    S._JOB_KILL_PENDING.discard(JOB)
    # 2026-11-01：收尾幂等键也必须清 —— 留着上一轮的 proc，收尾会被判成"已做过"，
    # 用例就会在"什么都没发生"的情况下变红/变绿。
    S._JOB_SETTLED.pop(JOB, None)


def _wait_settled(S, job: str = JOB, timeout: float = 3.0) -> None:
    """等 supervisor 线程把这一轮收尾落地（收尾是异步的，断言不能抢跑）。

    2026-11-01 变更：收尾改由 `_job_supervisor` 守护线程在子进程退出后**立刻**驱动，
    不再等"同一 job 下次被启动"。所以这些用例必须显式等它完成，
    否则断言的就不是行为，而是线程调度运气。
    """
    end = time.time() + timeout
    while time.time() < end:
        with S._JOB_SETTLE_LOCK:
            if S._JOB_SETTLED.get(job) is not None:
                return
        time.sleep(0.01)
    raise AssertionError("子进程收尾没有在 %.1fs 内发生（supervisor 没生效）" % timeout)


@pytest.fixture()
def sv(monkeypatch, tmp_path):
    """把 `viewer.server` 的调度状态清干净，并把日志目录指到 tmp。"""
    from viewer import server as S

    saved = {name: dict(getattr(S, name))
             for name in ("_JOB_PROCS", "_JOB_LAST", "_JOB_LAST_OK", "_JOB_SETTLED")}
    saved_warned = set(S._JOB_WARNED)
    saved_kill = set(S._JOB_KILL_PENDING)

    monkeypatch.setattr(S, "ROOT", str(tmp_path))   # sched-*.log 落 tmp，不污染 viewer/logs
    _clear_state(S)
    yield S

    # 2026-11-01：收尾在线程里跑，必须在 monkeypatch 撤销**之前** join 掉，
    # 否则线程会带着恢复后的生产 `BRAKE`/`ROOT` 继续收尾（污染进程级状态）。
    import threading
    for t in threading.enumerate():
        if t.name.startswith("job-supervisor-"):
            t.join(timeout=10)

    for name, value in saved.items():
        getattr(S, name).clear()
        getattr(S, name).update(value)
    S._JOB_WARNED.clear()
    S._JOB_WARNED.update(saved_warned)
    S._JOB_KILL_PENDING.clear()
    S._JOB_KILL_PENDING.update(saved_kill)


def _install(sv, monkeypatch, returncodes):
    """装桩：Popen 按 `returncodes` 依次返回假进程。

    返回 `(spawn 记录, 闸, backoff 记录, 退避窗口)`。最后一个是个**可变单元素列表**，
    测试可以通过 `wait[0] = 120.0` 模拟"刚才那次失败把退避窗口立起来了"。
    起始值必须是 0：`_spawn_offproc` 在退避窗口内会在进入 Popen 之前就 return False。
    """
    spawns: list[bytes | str] = []
    codes = list(returncodes)

    def fake_popen(argv, **kwargs):  # noqa: ARG001
        spawns.append(argv[-1])
        rc = codes[min(len(spawns) - 1, len(codes) - 1)]
        return _FakeProc(rc)

    monkeypatch.setattr(sv.subprocess, "Popen", fake_popen)

    brake = _FakeBrake()
    monkeypatch.setattr(sv, "BRAKE", brake)

    fails: list[str] = []
    monkeypatch.setattr(sv, "BRAKE_backoff_fail",
                        lambda job, err="": (fails.append(str(err)) or 60.0))
    wait = [0.0]
    monkeypatch.setattr(sv, "BRAKE_backoff_wait", lambda job: float(wait[0]))
    monkeypatch.setattr(sv, "BRAKE_backoff_ok", lambda job: None)
    return spawns, brake, fails, wait


# ---------------------------------------------------------------- 1. 核心：窗口过期能重开
def test_restarts_after_backoff_window_expires(sv, monkeypatch):
    """一次非零退出 → 记录被摘掉 → 窗口过期后**真的**再次 Popen。

    旧实现（不 pop）在这条上必红：第 3 次调用仍然撞到那条 `returncode=1` 的死记录，
    `Popen` 调用次数永远是 1。
    """
    spawns, brake, fails, _wait = _install(sv, monkeypatch, returncodes=[1])

    # ① 首次启动：成功放行（子进程后来非零退出）
    assert sv._spawn_offproc(JOB) is True
    assert len(spawns) == 1

    # ② 子进程退出后由 supervisor 立刻收尾（2026-11-01 起不再等下一次再入）：
    #    记失败、归还租约、摘掉死记录。这里显式等它落地，断言才不依赖线程调度运气。
    _wait_settled(sv)
    assert JOB not in sv._JOB_PROCS, (
        "失败分支没有摘掉 _JOB_PROCS —— 下个 tick 会再撞这条死记录，退避窗口被无限后推")
    assert fails == ["exit=1"], "应只记一次失败：%r" % (fails,)
    assert brake.released >= 1, "失败分支必须归还闸租约，否则写闸会被永久占死"

    # ③ 收尾之后同一 tick 内调一次：不得当场重开（交给退避窗口）
    assert sv._spawn_offproc(JOB) is False
    assert len(spawns) == 1, "失败后不当场重开（交给退避窗口）"

    # ④ 时间推进 999s（> _OFFPROC_GAP 默认 600s）、退避窗口已过期 → 必须能再起一个
    sv._JOB_LAST[JOB] -= 999
    assert sv._spawn_offproc(JOB) is True
    assert len(spawns) == 2, "退避窗口过期后没有重试 —— 退避从『有界等待』退化成『永久停摆』"


# ---------------------------------------------------------------- 2. 窗口内不许重开
def test_does_not_restart_inside_backoff_window(sv, monkeypatch):
    """退避窗口内**确实不启动**（修完以后仍然要遵守退避）。"""
    spawns, _brake, _fails, wait = _install(sv, monkeypatch, returncodes=[1])

    assert sv._spawn_offproc(JOB) is True          # 第一次放行
    sv._spawn_offproc(JOB)                         # 观察退出码 1 → 摘记录 + 记失败
    wait[0] = 120.0                                # 刚才那次失败把 60s 窗口立起来了
    sv._JOB_LAST[JOB] -= 999                       # 时间已远超进程间隔
    assert sv._spawn_offproc(JOB) is False, "退避窗口内不该启动"
    assert len(spawns) == 1, "窗口内被启动了 %d 次" % (len(spawns) - 1)


# ---------------------------------------------------------------- 3. 一次失败只记一次
def test_one_failure_is_recorded_once_not_per_tick(sv, monkeypatch):
    """连续 5 个 tick 只允许记 1 次失败。

    旧实现：5 个 tick → 5 次 `BRAKE_backoff_fail` → 退避阶梯被推到 1800s 封顶，
    而且每个 tick 都"重新开始"一次退避倒计时，永远等不到。
    """
    _spawns, _brake, fails, wait = _install(sv, monkeypatch, returncodes=[1])

    assert sv._spawn_offproc(JOB) is True          # 启动
    sv._spawn_offproc(JOB)                         # 记录失败
    wait[0] = 1800.0                               # 阶梯已推到封顶
    sv._JOB_LAST[JOB] -= 999
    for _ in range(5):
        assert sv._spawn_offproc(JOB) is False     # 5 个 tick 全被退避挡住
    assert len(fails) == 1, "一次失败被记了 %d 次（退避阶梯会被推到封顶）" % len(fails)


# ---------------------------------------------------------------- 4. 护栏：成功侧不摘
def test_success_branch_keeps_record_and_last_ok(sv, monkeypatch):
    """成功分支必须保留 `_JOB_PROCS`，并写下 `_JOB_LAST_OK`。

    为什么专门测：`_daily_job`（`viewer/server.py`）靠 `_JOB_LAST_OK[job]` 判断
    "上一轮已成功" → 才补写 `meta` 的当日完成标记。修复只允许动**失败侧**；
    若把 pop 也放到成功侧，记录被摘掉后 `prev.poll()` 再也看不到 0，这条链就断了。
    """
    spawns, _brake, fails, _wait = _install(sv, monkeypatch, returncodes=[0])

    assert sv._spawn_offproc(JOB) is True
    assert sv._spawn_offproc(JOB) is False         # 观察退出码 0
    _wait_settled(sv)                              # 收尾是 supervisor 线程驱动的，等它落地
    assert len(spawns) == 1
    assert fails == [], "成功不该记失败：%r" % (fails,)
    assert JOB in sv._JOB_PROCS, "成功分支不该摘记录（_JOB_LAST_OK 记账依赖它）"
    assert JOB in sv._JOB_LAST_OK, "成功分支必须写下 _JOB_LAST_OK（日更任务据此补写当日标记）"
    assert JOB not in sv._JOB_WARNED


# ---------------------------------------------------------------- 6. 红态自证
def test_dead_record_would_freeze_the_job_forever(sv, monkeypatch):
    """自证（反向验证）：把那条「死记录」手工放回 `_JOB_PROCS`（= 修复前的状态），
    任务就永久停摆 —— 每个 tick 都 return False 且每次都推一次退避。

    这条证明上面几条断言真的在测这个 bug，而不是"怎么写都绿"。
    它不依赖 git 历史、不改源码，只是复现修复前内存里那个形状。
    """
    spawns, _brake, fails, _wait = _install(sv, monkeypatch, returncodes=[1])

    sv._JOB_LAST[JOB] = 0                 # 就算间隔检查放行，也永远走不到它

    for _ in range(5):
        # 修复前这条记录**永远不会被摘**，所以每个 tick 进来看到的都是同一个死进程。
        # 这里每轮补回去，等价于复现那个"没有 pop"的旧行为。
        sv._JOB_PROCS[JOB] = _FakeProc(1)
        assert sv._spawn_offproc(JOB) is False
    assert len(spawns) == 0, "死记录在场时不该起进程"
    assert len(fails) == 5, (
        "死记录在场时每个 tick 都推一次退避（实测 5 tick → 5 次），"
        "退避窗口被无限后推 —— 这就是『一次失败==永久停摆』的机制本身")

# ---------------------------------------------------------------- 5. 结构护栏
def test_failure_branch_is_the_only_place_that_pops():
    """静态检查：`_JOB_PROCS.pop` 只出现在**失败**分支（成功分支不许有）。

    用源码里的相对位置判断，避免"某天有人顺手在成功分支也加一行 pop"这种回归。

    2026-11-01 变更：这段收尾逻辑从 `_spawn_offproc` 内联搬到了独立的
    `_job_settle()` —— 因为现在有三条路径会收尾（supervisor 线程 / 每 tick 的
    `_reap_finished_jobs` / 再入分支），必须**一份实现**。判据本身不变，只是被检查的
    函数换了；另外多查一条：全文件只允许一处 `_JOB_PROCS.pop(`（不许有第二份收尾实现）。
    """
    import inspect

    from viewer import server as S

    src = inspect.getsource(S._job_settle)
    fail_branch = src.index("_JOB_LAST_OK.pop(job, None)")      # 失败分支起点
    pop_at = src.index("_JOB_PROCS.pop(job, None)")
    ok_branch = src.index("_JOB_LAST_OK[job] = time.time()")    # 成功分支
    assert fail_branch < pop_at < ok_branch, (
        "`_JOB_PROCS.pop` 必须落在失败分支内、成功分支之前")
    assert inspect.getsource(S).count("_JOB_PROCS.pop(") == 1, (
        "`_JOB_PROCS.pop` 只应出现一次（`_job_settle` 的失败分支）—— "
        "出现第二处就说明有第二份收尾实现，幂等假设不成立了")
