# -*- coding: utf-8 -*-
"""P0 回归：子进程退出后，写域租约必须**立刻**归还（不能等"同一 job 下次被启动"）。

## 被测的 bug（2026-11-01 现场）

```
00:35:25  hotspots 子进程结束（实际耗时 61.2s，rc=0）
00:35:25  ~ 01:40  日志持续：lhb_daily / bili_scan / resonance_build /
                   selfpick_build / evolve_daily 本轮跳过：写域 app 被 hotspots 占用
```

`Brake.release()` 本身没写错，错在**调用时机**：唯一的 `BRAKE.release(job)` 在
`_spawn_offproc` 的「上一轮已结束」分支里 —— 也就是**只有这个 job 下次被尝试启动**时
才会执行到。于是：

  · job 进入 `_OFFPROC_GAP` 冷却（hotspots 是 600s）→ 这 600s 里没人 release；
  · 该 job 当天已跑完、不再被调度触发 → **永远**没人 release；
  · 结果写域被一个早已死透的进程占住，直到 brake 的兜底过期（旧口径 COMPUTE=7200s）。

同一个病灶还有第二处：`_JOB_TIMEOUT` 超时熔断与 `_JOB_KILL_PENDING` 升级 kill 的逻辑
也都在那个再入分支里 —— 同样"依赖下次调度"，因此一个卡死的任务同样能占住写域很久。

## 本文件的三条判据（对应修复的三块）

1. **收尾即刻发生**（`_job_supervisor`）：真起一个短命子进程，它退出后 **1 秒内**
   `BRAKE.acquire("lhb_daily")`（同写域、不同 job）必须成功。旧实现必红 —— 它只在
   再入时 release，而本用例**不会**再次 `_spawn_offproc("hotspots")`（那正是旧实现的触发条件）。
2. **tick 兜底**（`_reap_finished_jobs`）：即使 supervisor 没跑（历史记录 / 起线程失败），
   一个调度 tick 也要能回收。
3. **心跳**（`Brake.heartbeat` + `_reap_locked`）：`_STALE_AFTER` 语义已从"任务总时长上限"
   改为"**心跳**超时"。心跳持续 → 长任务不被误杀；心跳停止 → 300s 内回收；
   心跳再新鲜也不能突破 `_ABS_MAX` 绝对上限（防僵尸 supervisor 永久续命）。
4. **幂等**：同一次失败被收两遍（supervisor + tick 竞态）时，退避阶梯只许推进一档。

跑法：`.venv/Scripts/python.exe -m pytest tests/test_lease_release.py -q`

全部在 tmp 目录上跑（`ROOT` 指到 `tmp_path`），不碰生产日志、不碰生产库、不联网。
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _detach_prod_log_handlers() -> list:
    """摘掉根 logger 上的 FileHandler。

    教训见 `tests/test_offproc_backoff_retry.py`：导入 `viewer.server` 会在根 logger 上
    挂一个指向 `viewer/logs/server-<date>.log` 的 RotatingFileHandler，于是本文件里的
    每一行日志都会写进**生产日志**，污染事后排查。
    """
    root = logging.getLogger()
    removed = [h for h in list(root.handlers) if isinstance(h, logging.FileHandler)]
    for h in removed:
        root.removeHandler(h)
    return removed


from viewer import server as S            # noqa: E402
from viewer.kernel import brake as B      # noqa: E402

_DETACHED = _detach_prod_log_handlers()

# 与事故完全同名同域：hotspots 是 (COMPUTE, "app")，lhb_daily 是 (FETCH, "app")。
# 两者写域相同 → 天然互斥，正是事故里被挡住的形态。
JOB_A = "hotspots"
JOB_B = "lhb_daily"


class _FakeProc:
    """最小 Popen 替身：只有 `_job_settle` 真正需要的东西。

    刻意**不**提供 `wait()` —— 收尾路径一律走 `poll()`（supervisor 若用 `wait()` 阻塞，
    同一线程就没机会发心跳了，所以 poll 是契约的一部分）。
    """

    def __init__(self, returncode: int = 0, pid: int = 909090):
        self.returncode = returncode
        self.pid = pid
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1


@pytest.fixture()
def sv(monkeypatch, tmp_path):
    """隔离调度状态：独立 Brake 单例 + tmp 日志目录 + 清空 job 表。

    用独立的 `B.Brake()` 而不是全局单例：单例是进程级共享状态，别的测试留下的租约
    会让这里"莫名红"（`_MAX_TOTAL=1` 时尤其明显）。
    """
    saved = {name: dict(getattr(S, name))
             for name in ("_JOB_PROCS", "_JOB_LAST", "_JOB_LAST_OK", "_JOB_SETTLED",
                          "_JOB_META", "_BRAKE_BLOCK_REASON")}
    saved_warned = set(S._JOB_WARNED)
    saved_kill = set(S._JOB_KILL_PENDING)

    monkeypatch.setattr(S, "ROOT", str(tmp_path))     # sched-*.log 落 tmp
    monkeypatch.setattr(S, "BRAKE", B.Brake())        # 独立闸：测试之间不互相污染
    S._JOB_PROCS.clear()
    S._JOB_LAST.clear()
    S._JOB_LAST_OK.clear()
    S._JOB_SETTLED.clear()
    S._JOB_META.clear()
    S._BRAKE_BLOCK_REASON.clear()
    S._JOB_WARNED.clear()
    S._JOB_KILL_PENDING.clear()
    # 退避状态是模块级全局，按本文件用的 job 名清一遍（不整体清，避免影响并发用例）
    for j in (JOB_A, JOB_B, "lease_idem_probe"):
        B.backoff_ok(j)

    yield S

    # 收尾必须排在 monkeypatch 撤销**之前**：supervisor 是 daemon 线程，
    # 若它活到撤销之后，看到的 `BRAKE` / `ROOT` 又变回生产对象了。
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
    for j in (JOB_A, JOB_B, "lease_idem_probe"):
        B.backoff_ok(j)


# ================================================================
# 1. 核心：子进程退出 → 1 秒内归还写域（旧实现必红）
# ================================================================
def test_lease_is_released_within_one_second_after_child_exits(sv, monkeypatch):
    """真起一个短命子进程：它退出之后，同写域的另一个任务必须能在 1 秒内拿到租约。

    ⚠️ 本用例**不会**再次调用 `_spawn_offproc(JOB_A)` —— 那正是旧实现的触发条件。
    旧实现（只在再入时 release）在这里只能靠 7200s 兜底过期，必红。
    """
    spawned: list = []
    real_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):  # noqa: ARG001
        # 用真实的 Popen 起一个立刻退出的进程：supervisor 线程必须真的观察到退出，
        # 而不是对着桩对象的常量 returncode 自说自话。
        p = real_popen([sys.executable, "-c", "pass"],
                       stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"),
                       stdin=subprocess.DEVNULL, cwd=kwargs.get("cwd"))
        spawned.append(p)
        return p

    monkeypatch.setattr(sv.subprocess, "Popen", fake_popen)

    t_spawn = time.time()
    assert sv._spawn_offproc(JOB_A) is True, "第一个任务应被放行"
    assert len(spawned) == 1
    proc = spawned[0]

    # 写域此刻确实被占住（不是"本来就没占"，那样断言没有说服力）
    assert sv.BRAKE.acquire(JOB_B) is None, (
        "前置条件不成立：%s 还没占住写域 app，后面的断言就测不到东西" % JOB_A)

    # 等它退出（只等退出，不等收尾）
    exit_deadline = time.time() + 10
    while proc.poll() is None and time.time() < exit_deadline:
        time.sleep(0.01)
    t_exit = time.time()
    assert proc.poll() is not None, "子进程 10 秒还没退出，用例前提被破坏"

    # 开始计时：从"观察到子进程已退出"起算，1 秒内必须能拿到同域租约
    lease = None
    hard_deadline = t_exit + 2.5
    while lease is None and time.time() < hard_deadline:
        lease = sv.BRAKE.acquire(JOB_B)
        if lease is None:
            time.sleep(0.01)
    elapsed = time.time() - t_exit

    assert lease is not None, (
        "子进程已退出 %.1fs，写域 app 仍被 %s 占着 —— 租约没有在进程退出时归还"
        "（这正是 P0 事故：后续任务全部空转到兜底超时）" % (elapsed, JOB_A))
    assert elapsed < 1.0, (
        "租约归还太慢：%.3fs（要求 <1s）。supervisor 的轮询间隔是 %.1fs，"
        "不该超过它 + 一点调度余量" % (elapsed, S._SUPERVISE_POLL_SEC))

    # 收尾的账要记全：成功分支必须留下 _JOB_LAST_OK（日更任务靠它补写"当日已完成"）
    assert JOB_A in sv._JOB_LAST_OK, "成功收尾没有写 _JOB_LAST_OK"
    assert JOB_A in sv._JOB_PROCS, "成功分支应保留记录（回归护栏：别把成功侧也摘了）"
    assert sv._JOB_SETTLED.get(JOB_A) is proc, "收尾幂等键没记到这一轮进程上"
    assert time.time() - t_spawn < 5.0, "子进程整体耗时异常，检查用例环境"

    sv.BRAKE.release(JOB_B)


def test_red_state_without_supervisor_domain_stays_locked(sv, monkeypatch):
    """红态自证 + tick 兜底：把 supervisor 摘掉（= 2026-11-01 之前的形态），写域就被死进程占住。

    这条证明上面那条断言真的在测 supervisor，而不是"怎么写都绿"：
      · 摘掉 supervisor → 子进程退出 1.2s 后，同域任务**仍然拿不到**租约（旧形态）；
      · 再调一次 `_reap_finished_jobs()`（tick 兜底）→ 立刻拿到。
    不依赖 git 历史、不改源码，只改一个符号的绑定。
    """
    real_popen = subprocess.Popen
    spawned: list = []

    def fake_popen(argv, **kwargs):  # noqa: ARG001
        p = real_popen([sys.executable, "-c", "pass"],
                       stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"),
                       stdin=subprocess.DEVNULL, cwd=kwargs.get("cwd"))
        spawned.append(p)
        return p

    monkeypatch.setattr(sv.subprocess, "Popen", fake_popen)
    # 摘掉 supervisor：线程还是会起来，但一行活都不干（等价于"没有收尾驱动"）
    monkeypatch.setattr(sv, "_job_supervisor", lambda job, proc, pid: None)

    assert sv._spawn_offproc(JOB_A) is True
    proc = spawned[0]
    deadline = time.time() + 10
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.01)
    assert proc.poll() is not None
    time.sleep(1.2)                     # 给"本该发生的收尾"充分的时间（旧形态下它不存在）

    assert sv.BRAKE.acquire(JOB_B) is None, (
        "supervisor 都摘掉了，租约却还是归还了 —— 说明断言没测到 supervisor 这条路径")
    assert sv._reap_finished_jobs() == 1, "tick 兜底没认出这条已退出的记录"
    assert sv.BRAKE.acquire(JOB_B) is not None, "tick 兜底必须能把红态救回来"
    sv.BRAKE.release(JOB_B)


def test_release_does_not_need_another_spawn_of_the_same_job(sv):
    """结构护栏：收尾的驱动者必须是 supervisor，不能只挂在再入分支上。

    这条把事故的形状固定成断言 —— 只要有人把 `Thread(target=_job_supervisor...)` 删了，
    收尾就会退化成「只有同 job 下次启动才归还」，也就是 2026-11-01 那次事故。
    """
    import inspect

    src = inspect.getsource(S._spawn_offproc)
    assert "Thread(target=_job_supervisor" in src, (
        "_spawn_offproc 里没有启动 supervisor 线程 —— 租约又会退化成"
        "「只有同 job 下次启动才归还」")
    assert src.index("subprocess.Popen(") < src.index("Thread(target=_job_supervisor"), (
        "supervisor 必须在子进程起来**之后**启动（否则会对着 None 发心跳）")


# ================================================================
# 2. tick 兜底：supervisor 没跑也要能回收
# ================================================================
def test_tick_reaper_releases_lease_when_supervisor_missing(sv):
    """`_reap_finished_jobs()` 是 supervisor 之外的第二道回收路径。

    为什么需要它：`_JOB_PROCS` 里可能有父进程重启前留下的历史记录，
    也可能出现线程创建失败的极端情况。一个 tick（10s）内必须兜住。
    """
    lease = sv.BRAKE.acquire(JOB_A)
    assert lease is not None
    dead = _FakeProc(returncode=0)
    sv._JOB_PROCS[JOB_A] = dead            # 直接塞一条"已退出但没被收尾"的记录
    sv._JOB_LAST[JOB_A] = time.time()

    assert sv.BRAKE.acquire(JOB_B) is None, "前置条件：此刻写域被占"

    assert sv._reap_finished_jobs() == 1, "tick 回收没有认出这条已退出的记录"

    other = sv.BRAKE.acquire(JOB_B)
    assert other is not None, (
        "tick 兜底没生效：子进程已退出、记录还在表里，写域却依然被占 —— "
        "这正是 supervisor 失效时的 P0 形态")
    sv.BRAKE.release(JOB_B)

    # 再跑一次必须幂等（不能每 tick 都重复记失败/重复告警）
    assert sv._reap_finished_jobs() == 0, "重复回收不该再记一次收尾"


def test_tick_reaper_enforces_timeout_for_all_running_jobs(sv):
    """超时熔断也必须脱离"同 job 再入" —— 否则卡死进程能一直占着写域。

    旧实现里 terminate/kill 只在 `_spawn_offproc(job)` 被再次调用时检查，
    而卡死的 job 恰恰因为租约还在、间隔未到，很可能永远等不到那一次调用。
    """
    assert sv.BRAKE.acquire(JOB_A) is not None
    alive = _FakeProc(returncode=None)
    alive.poll = lambda: None                     # 假装一直活着
    sv._JOB_PROCS[JOB_A] = alive
    sv._JOB_LAST[JOB_A] = time.time() - 999999    # 早已超过 _JOB_TIMEOUT

    sv._reap_finished_jobs()
    assert alive.terminated == 1, "超时后应发送 terminate"
    assert JOB_A in sv._JOB_KILL_PENDING, "应记下待升级 kill"

    sv._reap_finished_jobs()
    assert alive.killed == 1, "下一 tick 仍活着应升级为 kill"

    # 熔断期间**不许**归还租约：进程没死透，放第二个任务进来会抢同一份写锁
    assert sv.BRAKE.acquire(JOB_B) is None, (
        "进程还活着就把写域放给了别人 —— 会变成两个进程同时写 app 库")


# ================================================================
# 3. 心跳：语义从"总时长上限"改为"心跳超时"
# ================================================================
class _FakeClock:
    """只替换 `brake` 模块看到的时钟（`time.time/strftime/localtime`）。

    ⚠️ 不能直接 monkeypatch `time.time`（那会把整个进程的时间都改掉）。
    这里替换的是 `B.time` 这个**模块引用**，影响面严格限制在 brake 内部。
    """

    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def time(self) -> float:
        return self.now

    def strftime(self, fmt, tm=None):
        return time.strftime(fmt, tm)

    def localtime(self, t=None):
        return time.localtime(t)


def test_heartbeat_stops_means_lease_is_reaped(monkeypatch):
    """不心跳 → 心跳超时后 `_reap_locked` 必须释放（否则又会退化成永久占用）。"""
    clock = _FakeClock()
    monkeypatch.setattr(B, "time", clock)

    br = B.Brake()
    assert br.acquire(JOB_A) is not None

    clock.now += B._HEARTBEAT_TIMEOUT - 10          # 还差 10s 到超时
    br._reap_locked()
    assert br.status()["running_count"] == 1, "还没到心跳超时就回收了（会误杀长任务）"

    clock.now += 20                                 # 越过心跳超时
    br._reap_locked()
    assert br.status()["running_count"] == 0, (
        "心跳已停超过 %ds，租约仍在 —— 失联租约必须被回收" % B._HEARTBEAT_TIMEOUT)


def test_continuous_heartbeat_keeps_long_job_alive(monkeypatch):
    """持续心跳 → 即便跑得比心跳超时久得多，也不许回收（这是"不误杀长任务"的判据）。"""
    clock = _FakeClock()
    monkeypatch.setattr(B, "time", clock)

    br = B.Brake()
    lease = br.acquire(JOB_A)
    assert lease is not None
    br.bind_pid(lease, 4321)

    step = max(1.0, B._HEARTBEAT_TIMEOUT / 3.0)
    total = 0.0
    while total < B._HEARTBEAT_TIMEOUT * 5:          # 跑到 5 倍心跳超时
        clock.now += step
        total += step
        assert br.heartbeat(JOB_A, 4321) is True, "心跳应刷新到租约上"
        br._reap_locked()
        assert br.status()["running_count"] == 1, (
            "心跳一直新鲜（每 %.0fs 一次）却被回收了 —— 长任务会被误杀，"
            "indicators_daily 实测 2965s" % step)


def test_abs_max_caps_even_a_heartbeating_lease(monkeypatch):
    """绝对上限：心跳再新鲜也不能超过 `_ABS_MAX`（防僵尸 supervisor 永久续命）。"""
    clock = _FakeClock()
    monkeypatch.setattr(B, "time", clock)

    br = B.Brake()
    assert br.acquire(JOB_A) is not None

    step = max(1.0, B._HEARTBEAT_TIMEOUT / 3.0)
    total = 0.0
    while total < B._ABS_MAX + step:
        clock.now += step
        total += step
        br.heartbeat(JOB_A)          # 心跳从不间断
        br._reap_locked()

    assert br.status()["running_count"] == 0, (
        "心跳从未中断，租约却活过了 _ABS_MAX(%ds) —— 绝对上限没生效" % B._ABS_MAX)


def test_heartbeat_ignores_stale_pid(monkeypatch):
    """迟到的心跳不能给"别人的新租约"续命（pid 不符则不刷新）。"""
    clock = _FakeClock()
    monkeypatch.setattr(B, "time", clock)

    br = B.Brake()
    lease = br.acquire(JOB_A)
    br.bind_pid(lease, 1111)
    assert br.heartbeat(JOB_A, 1111) is True
    assert br.heartbeat(JOB_A, 2222) is False, "旧进程的心跳不该刷新新租约"
    assert br.heartbeat("不存在的job", 1111) is False


# ================================================================
# 4. 幂等：同一次失败只许推进一档退避
# ================================================================
def test_settle_is_idempotent_and_bumps_backoff_only_once(sv):
    """supervisor 与 tick 竞态下，同一份失败会被收两遍 —— 退避阶梯只许推一档。

    这条直接对应 2026-10-30 那次「一次失败 = 当天永久停摆」：重复
    `BRAKE_backoff_fail` 会把 60→300→900→1800 一路推到封顶，而每个 tick 又重新开始
    倒计时，于是**永远等不到重试**。
    """
    job = "lease_idem_probe"
    B.backoff_ok(job)
    dead = _FakeProc(returncode=1)
    sv._JOB_PROCS[job] = dead
    sv._JOB_LAST[job] = time.time()

    assert sv._job_settle(job, dead, via="first") is False, "失败收尾应返回 False（不当场重开）"
    st1 = B.backoff_status().get(job) or {}
    assert st1.get("fails") == 1, "第一次收尾应记 1 次失败：%r" % (st1,)

    # 再收一次（模拟 supervisor 与 tick 都跑到了 / 下一 tick 又撞见同一份记录）
    sv._job_settle(job, dead, via="second")
    sv._job_settle(job, dead, via="third")
    st2 = B.backoff_status().get(job) or {}
    assert st2.get("fails") == 1, (
        "同一轮失败被记了 %r 次 —— 退避阶梯会被推到封顶，任务再也重试不了" % (st2.get("fails"),))
    assert st2.get("wait_sec", 0) <= st1.get("wait_sec", 0), (
        "退避窗口被第二次收尾又推后了：%r → %r" % (st1, st2))
    assert job not in sv._JOB_PROCS, "失败分支必须摘掉死记录（否则下个 tick 又撞它）"
    B.backoff_ok(job)


def test_settle_is_idempotent_for_success_too(sv):
    """成功侧同理：重复收尾不能把 `_JOB_LAST_OK` 的戳往前推（那会让"当日已完成"的判断漂移）。"""
    job = "lease_idem_probe"
    proc = _FakeProc(returncode=0)
    sv._JOB_PROCS[job] = proc
    sv._JOB_LAST[job] = time.time()

    assert sv._job_settle(job, proc, via="first") is True
    ok_at = sv._JOB_LAST_OK[job]
    assert job in sv._JOB_PROCS, "成功分支必须保留记录（_daily_job 靠 _JOB_LAST_OK 记账）"

    time.sleep(0.02)
    assert sv._job_settle(job, proc, via="second") is True
    assert sv._JOB_LAST_OK[job] == ok_at, "重复收尾把成功时间戳又刷新了一次"


# ================================================================
# 5. /api/health 的 leases 段（观测面）
# ================================================================
def test_health_exposes_leases_and_blocked_by(sv):
    """`api_health()['leases']` 必须能回答"谁占着写域、pid 是否已死、谁在等"。"""
    lease = sv.BRAKE.acquire(JOB_A)
    assert lease is not None
    sv.BRAKE.bind_pid(lease, 777)
    dead = _FakeProc(returncode=0, pid=777)
    sv._JOB_PROCS[JOB_A] = dead                   # 故意造成"租约还在但子进程已死"

    # 模拟调度侧刚被挡过一次（这是 blocked_by 的数据来源）
    sv._BRAKE_BLOCK_REASON[JOB_B] = (time.time(), "写域 app 被 hotspots 占用")

    out = sv._brake_leases_view()
    assert out["ok"] is True
    items = out["items"]
    assert len(items) == 1
    it = items[0]
    for key in ("job", "pid", "started_at", "last_heartbeat", "age_sec",
                "heartbeat_age_sec", "kind", "domain", "proc_alive"):
        assert key in it, "leases 段缺字段 %s：%r" % (key, it)
    assert it["job"] == JOB_A and it["pid"] == 777
    assert it["domain"] == "app" and it["kind"] == "compute"
    assert it["proc_alive"] is False, (
        "proc_alive 必须能指出「租约还在、进程已死」—— 这正是那次事故的现场")

    blocked = {b["job"]: b for b in out["blocked_by"]}
    assert JOB_B in blocked, "被写域挡住的任务没进 blocked_by：%r" % (out["blocked_by"],)
    assert blocked[JOB_B]["holder"] == JOB_A
    assert blocked[JOB_B]["blocked_by_domain"] is True
    assert blocked[JOB_B]["domain"] == "app"


def test_health_structure_unchanged_except_new_section(sv, monkeypatch):
    """护栏：`leases` 只是**新增**字段，既有 health 结构不许被改。

    前端「系统状态」面板与 `tests/test_health.py` 都依赖既有键；事故修复不能顺手改接口。
    `HEALTH.overall()` 被打桩 —— 真跑它要扫库，与本用例要验的"结构"无关。
    """
    stub = {"ok": True, "level": "fresh", "reasons": [], "jobs": {}, "freshness": {}}
    monkeypatch.setattr(sv.HEALTH, "overall", lambda *a, **k: dict(stub))

    out = sv.api_health()
    for key in ("ok", "level", "reasons", "jobs", "freshness"):
        assert key in out, "api_health 丢了既有字段 %s（只允许新增 leases 段）" % key
    assert out["level"] == "fresh", "既有字段被改动"
    assert "leases" in out, "api_health 没有暴露 leases 段"
    assert out["leases"]["ok"] is True
