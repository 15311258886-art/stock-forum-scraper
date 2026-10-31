"""viewer/kernel/brake.py — 全局作业刹车闸（准入控制）

======================= 为什么需要它（实测证据，非推测） =======================
2026-10-29 17:42:45，调度器在同一 tick 内一次性放行 6 个重计算子进程：
    kbase_daily / indicators_daily / kline_daily / universe_scan /
    strategy_templates / a_shares_snapshot
后果（server-20261029.log 原文）：
  · kbase_daily 进程 RSS 冲到 3.9 GB（要读 2.3GB 的 kbase.db）
  · indicators_daily 上一轮实测 2965.0s（49 分钟），本轮又开跑
  · universe_scan 抛 sqlite3.OperationalError: database is locked
  · 该时段 GET /api/refresh_status 耗时 246505 ms（246 秒）
  · 前端 15 秒硬超时 → 横幅「正在自动恢复 第2/3次」→ 用户看到「正在加载数据…」永不回来

原有的两道补丁为什么挡不住：
  · _spawn_offproc 只有「同一 job 不重入 + 该 job 自己的 gap」——六个任务各自都"合法"，
    但它们叠加起来就是一台机器的资源雪崩；缺的是**全局**视角。
  · _idle_gate 只在任务启动那一瞬间看用户是否活跃；一旦放行，跑 49 分钟期间用户回来了
    照样全站冻结。它是"排队礼让"，不是"并发上限"。

============================ 本闸的三条硬规矩 ============================
  1. 全局并发上限（按类别配额）：重计算最多 1 个、网络抓取最多 1 个、轻任务最多 2 个。
  2. 写域互斥：声明了同一 DB 写域（app / kbase）的任务串行，根治 database is locked。
  3. 拿不到许可 → **立刻跳过本轮**，绝不阻塞调用方。
     （调度线程、请求线程都不能等；一个"会等的闸"会变成新的冻结源。）

============================= 能力边界（不吹） =============================
本闸是**准入控制**，不是**实时抢占**：已经在跑的进程不会被它中断。
因此配额刻意设得保守（重计算=1），用"排队"换"永不雪崩"。
租约（lease）带心跳超时，即便调用方忘记 release（进程被强杀、父进程重启），
过期后自动失效，不会把闸永久锁死。

=================== 2026-11-01 P0：租约泄漏（心跳机制，别再改回去）===================
事故：`hotspots` 在 00:35:25 已跑完（实际 61.2s），此后一个多小时日志持续显示
  `lhb_daily / bili_scan / resonance_build / selfpick_build / evolve_daily`
  因「写域 app 被 hotspots 占用」而跳过。

根因不在 release 的实现（`pop(job, None)` 是对的），而在**调用时机**：
  旧代码里唯一的 `BRAKE.release(job)` 位于 `_spawn_offproc` 的「上一轮已结束」分支，
  即**只有该 job 被再次尝试启动时**才归还租约。于是当该 job 进入 `_OFFPROC_GAP`
  冷却（hotspots 是 600s）、或当天已跑完不再被调度触发时，**没有任何路径去 release**，
  写域就被一个已死进程占住，直到旧口径 `_STALE_AFTER[COMPUTE] = 7200s` 兜底过期。

两道修法（缺一不可）：
  1. 子进程退出即刻回收 —— 父进程给每个子进程配 daemon supervisor 线程，
     退出后原子完成 release / 退避记账 / 状态标记（见 viewer/server.py `_job_settle`）；
     调度 tick 另有 `_reap_finished_jobs()` 全量兜底。
  2. 租约心跳 —— 本文件新增 `heartbeat()`。`_STALE_AFTER` 的语义因此从
     「任务总时长上限」改成「**心跳**超时」，默认值从 7200s 降到 300s。
     为什么能降这么狠而不会误杀长任务：心跳由**父进程的 supervisor 线程**每 ~5s 发一次，
     与任务本身跑多久无关（`indicators_daily` 跑 2965s 照样心跳新鲜）。
     「心跳停了」等价的正是「没有人在监管这个子进程」—— 那才是该回收的时刻。
     另外保留 `_ABS_MAX`（默认 7200s）绝对上限，防止一个僵尸 supervisor 永久续命。
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 类别与配额
# 重计算：吃 CPU / 内存，纯 Python 或 numpy/pandas 密集
COMPUTE = "compute"
# 网络抓取：以 IO 等待为主，但会集中写库
FETCH = "fetch"
# 轻任务：秒级、可与其他任务共存
LIGHT = "light"

# 配额（同时最多几个）。可用环境变量覆盖，便于临时排障。
_DEFAULT_QUOTA = {
    COMPUTE: int(os.environ.get("BRAKE_COMPUTE", "1")),
    FETCH: int(os.environ.get("BRAKE_FETCH", "1")),
    LIGHT: int(os.environ.get("BRAKE_LIGHT", "2")),
}

# 总闸：COMPUTE + FETCH 合计同时最多几个（LIGHT 不计入，它几乎不吃资源）。
# 默认 1 —— 实测教训：kbase_daily 单个进程 RSS 就 3.9GB，它和 indicators_daily 并行时
# 磁盘/内存/SQLite 写锁同时饱和，refresh_status 要等 246 秒。宁可排队，不可雪崩。
# 机器够强可设 BRAKE_MAX_TOTAL=2 放开一路并行。
_MAX_TOTAL = int(os.environ.get("BRAKE_MAX_TOTAL", "1"))

# 租约「心跳」超时（秒）：超过这么久**没有收到心跳**，才判定调用方已失联并自动释放。
#
# 2026-11-01 语义变更（P0 租约泄漏）：原口径是「任务总时长上限」，必须 >= 最慢任务
# （indicators_daily 实测 2965s），因此 COMPUTE 给到 7200s。代价是——**兜底期本身成了故障窗口**：
# 父进程忘记 release（或 supervisor 没起来）时，写域会被一个早就死透的进程占住整整 2 小时。
# 实测事故：hotspots 61s 跑完，写域 app 被占到超时，其间 lhb_daily / bili_scan /
# resonance_build / selfpick_build / evolve_daily 全部空转一个多小时。
#
# 新口径：父进程每 ~5s 给在跑的子进程发一次心跳（`_SUPERVISE_HB_SEC`，见
# viewer/server.py `_job_supervisor`），所以「心跳新鲜」= 「还有人在监管这个子进程」，
# 与任务跑多久无关 —— 长任务不会被误杀。
# 真失联（父进程被杀 / supervisor 异常死亡）的租约 300s 内一定被回收。
_HEARTBEAT_TIMEOUT = int(os.environ.get("BRAKE_HEARTBEAT_TIMEOUT", "300"))

_STALE_AFTER = {
    COMPUTE: _HEARTBEAT_TIMEOUT,
    FETCH: _HEARTBEAT_TIMEOUT,
    LIGHT: _HEARTBEAT_TIMEOUT,
}

# 绝对上限（秒）：心跳再新鲜也不能让一个租约活过这么久。
# 为什么要有它：心跳是**父进程自己发的**，万一 supervisor 线程陷入「poll 恒返回 None」的
# 假活状态（或有人给心跳开了后门），没有这道闸就是一个永不过期的租约 —— 比原 bug 更糟。
_ABS_MAX = int(os.environ.get("BRAKE_ABS_MAX", "7200"))

# ---------------------------------------------------------------- 任务画像
# job -> (类别, 写域)。写域相同的任务互斥串行。
# 未登记的任务按 (COMPUTE, "app") 处理 —— 宁可保守排队，也不放一个未知重活进池。
_JOBS: dict[str, tuple[str, str]] = {
    # —— 重计算：全市场因子 / 模型 / 聚合 ——
    "indicators_daily":   (COMPUTE, "app"),
    "universe_scan":      (COMPUTE, "app"),
    "strategy_templates": (COMPUTE, "app"),
    "predictor_daily":    (COMPUTE, "app"),
    "pillars_daily":      (COMPUTE, "app"),
    "resonance_build":    (COMPUTE, "app"),
    "selfpick_build":     (COMPUTE, "app"),
    "evolve_daily":       (COMPUTE, "app"),
    "signal_backtest":    (COMPUTE, "app"),
    # 2026-10-30：blogger_perf / blogger_daily / blogger_score_daily 三个任务已随博主荐股下线而删除
    "kline_prewarm":      (COMPUTE, "kline"),
    "quant":              (COMPUTE, "app"),
    "etf_payload":        (COMPUTE, "app"),
    "kronos_payload":     (COMPUTE, "app"),
    "variant_payload":    (COMPUTE, "app"),
    "hotspots":           (COMPUTE, "app"),
    "bottom_scan":        (COMPUTE, "app"),
    # —— 网络抓取：以 IO 等待为主，集中写库 ——
    "kline_daily":        (FETCH, "kline"),
    # 2026-10-29 修正分类：kbase_daily 虽以抓 K 线为主，但它要读 2.3GB 的 kbase.db，
    # 实测 RSS 冲到 3.9GB —— 按「内存重度」归 COMPUTE，绝不与别的重活并行。
    "kbase_daily":        (COMPUTE, "kbase"),
    "a_shares_snapshot":  (FETCH, "app"),
    "intraday_snap":      (FETCH, "app"),
    "douyin_corpus":      (FETCH, "app"),
    "bili_scan":          (FETCH, "app"),
    "lhb_daily":          (FETCH, "app"),
    # 2026-10-30：板块资金流日线日更。以 HTTP 收数为主（东财 datacenter 报表，一天 4 个请求），
    # 没有 CPU 密集计算，故归 FETCH 而不是未登记时的默认 (COMPUTE, "app")。
    # 写域仍是 app（写 board_flow_daily），因此天然与 lhb_daily / a_shares_snapshot 串行。
    "board_flow_range_daily": (FETCH, "app"),
    "blogger_daily":      (FETCH, "app"),
    "plate_members":      (FETCH, "app"),
    "build_exclusion":    (LIGHT, "app"),
    "watchdesk_heartbeat": (LIGHT, "app"),
    # —— AI 分析：调外部模型是网络等待，但与用户请求抢同一份信息包读取，归 LIGHT ——
    "ai_review":          (LIGHT, "ai"),
}


def profile(job: str) -> tuple[str, str]:
    """返回 (类别, 写域)。未登记任务按最保守口径处理。"""
    return _JOBS.get(job, (COMPUTE, "app"))


class _Lease:
    """一次准入许可。

    `last_heartbeat` 是 2026-11-01 新增的心跳戳（初始化 = `started_at`）：
    只要监管子进程的父线程还活着，它就每 ~5s 被刷新一次；停止刷新即视为失联。
    `settled` 标记「这轮收尾已经做过了」—— 给调用方的收尾流程做幂等（重复 release 无害，
    但重复记失败会把指数退避窗口一次次往后推，那是有害的）。
    """

    __slots__ = ("job", "kind", "domain", "pid", "started_at", "last_heartbeat",
                 "reason", "settled")

    def __init__(self, job: str, kind: str, domain: str):
        self.job = job
        self.kind = kind
        self.domain = domain
        self.pid: int | None = None
        self.started_at = time.time()
        self.last_heartbeat = self.started_at
        self.reason = ""
        self.settled = False

    def to_dict(self) -> dict:
        now = time.time()
        return {
            "job": self.job,
            "kind": self.kind,
            "domain": self.domain,
            "pid": self.pid,
            # 可读时间 + 秒数两套口径都给：人看前者，机器/前端排序用后者
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
            "last_heartbeat": time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime(self.last_heartbeat)),
            "age_sec": int(now - self.started_at),
            "heartbeat_age_sec": int(now - self.last_heartbeat),
            "settled": bool(self.settled),
        }


class Brake:
    """进程内单例：全局作业准入闸。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, _Lease] = {}   # job -> lease
        self._quota = dict(_DEFAULT_QUOTA)
        # 观测计数（给诊断接口用，让人能看见"谁在占坑"）
        self._rejected: dict[str, int] = {}
        # 2026-10-29：被挡日志限频（job -> 上次打印时刻）。
        # 实测：调度线程每 10 秒 tick 一次，一次 tick 有 7 个任务被挡 → 每 10 秒刷 7 行，
        # 一小时 2500 行，日志反而成了新的噪声源，把真正的信息埋掉。
        self._last_log: dict[str, float] = {}

    def should_log(self, job: str, gap: float = 120.0) -> bool:
        """同一 job 的『本轮跳过』最多每 gap 秒记一条。"""
        now = time.time()
        with self._lock:
            last = self._last_log.get(job, 0.0)
            if now - last < gap:
                return False
            self._last_log[job] = now
            return True

    # ------------------------------------------------------------ 内部
    def _reap_locked(self) -> None:
        """清理失联租约（父进程被强杀 / supervisor 没起来 / 心跳停止）。

        判据（2026-11-01 起）：`now - last_heartbeat > 心跳超时`，
        而不是旧的 `now - started_at > 任务总时长上限`。
        再加一道 `_ABS_MAX` 绝对上限，防止「心跳被僵尸线程续命」把租约变成永久。
        """
        now = time.time()
        for job, ls in list(self._leases.items()):
            age = now - ls.started_at
            hb_age = now - ls.last_heartbeat
            if hb_age > _STALE_AFTER.get(ls.kind, _HEARTBEAT_TIMEOUT):
                log.warning("[brake] 租约心跳超时自动释放 %s（心跳已停 %ds，租约已 %ds，pid=%s）",
                            job, int(hb_age), int(age), ls.pid)
                self._leases.pop(job, None)
            elif age > _ABS_MAX:
                log.warning("[brake] 租约超过绝对上限 %ds 自动释放 %s（pid=%s，心跳 %ds 前）",
                            _ABS_MAX, job, ls.pid, int(hb_age))
                self._leases.pop(job, None)

    def _count_locked(self, kind: str) -> int:
        return sum(1 for ls in self._leases.values() if ls.kind == kind)

    def _domain_busy_locked(self, domain: str, job: str) -> str:
        for other, ls in self._leases.items():
            if other != job and ls.domain and ls.domain == domain:
                return other
        return ""

    # ------------------------------------------------------------ 对外
    def acquire(self, job: str) -> _Lease | None:
        """尝试取得执行许可。成功返回 lease，失败返回 None（调用方应跳过本轮）。"""
        kind, domain = profile(job)
        with self._lock:
            self._reap_locked()

            # 同一 job 已在池中 → 不重入
            if job in self._leases:
                self._rejected[job] = self._rejected.get(job, 0) + 1
                return None

            # 写域互斥：同域已有任务在写 → 排队等下一轮（不进池）
            holder = self._domain_busy_locked(domain, job)
            if holder:
                self._rejected[job] = self._rejected.get(job, 0) + 1
                log.debug("[brake] %s 让位：写域 %s 被 %s 占用", job, domain, holder)
                return None

            # 总闸：COMPUTE + FETCH 合计上限（LIGHT 不计入 —— AI 分析等用户主动任务永不饿死）
            if kind in (COMPUTE, FETCH):
                heavy = self._count_locked(COMPUTE) + self._count_locked(FETCH)
                if heavy >= _MAX_TOTAL:
                    self._rejected[job] = self._rejected.get(job, 0) + 1
                    log.debug("[brake] %s 让位：总闸已满（%d/%d）", job, heavy, _MAX_TOTAL)
                    return None

            # 类别配额
            if self._count_locked(kind) >= self._quota.get(kind, 1):
                self._rejected[job] = self._rejected.get(job, 0) + 1
                log.debug("[brake] %s 让位：%s 配额已满（%d）", job, kind, self._quota.get(kind, 1))
                return None

            ls = _Lease(job, kind, domain)
            self._leases[job] = ls
            return ls

    def bind_pid(self, lease: _Lease | None, pid: int) -> None:
        if lease is None:
            return
        with self._lock:
            ls = self._leases.get(lease.job)
            if ls is not None:
                ls.pid = int(pid)

    def release(self, job: str) -> None:
        with self._lock:
            self._leases.pop(job, None)

    def heartbeat(self, job: str, pid: int | None = None) -> bool:
        """给租约续命。返回 True 表示确实刷新了某个租约。

        谁发心跳：**父进程的 supervisor 线程**（`viewer/server.py::_job_supervisor`），
        每 ~5s 一次，代表「我还在监管这个子进程」。
        ⚠️ 这不是子进程自己在报到：子进程假死（卡在某行代码里）时，父进程照样心跳 ——
        那类僵死由 `_JOB_TIMEOUT` 超时熔断负责 terminate/kill，两套机制分工不同。

        `pid` 传了就校验：租约已经被回收并被**另一次运行**重新占用时，
        迟到的心跳不能把新租约的账记到旧进程头上。
        """
        with self._lock:
            ls = self._leases.get(job)
            if ls is None:
                return False
            if pid is not None and ls.pid is not None and int(ls.pid) != int(pid):
                return False
            ls.last_heartbeat = time.time()
            return True

    def mark_settled(self, job: str) -> None:
        """标记租约「本轮收尾已做」，供调用方判断是否重复收尾（幂等护栏）。"""
        with self._lock:
            ls = self._leases.get(job)
            if ls is not None:
                ls.settled = True

    def why(self, job: str) -> str:
        """给日志一句人话：为什么被挡。"""
        kind, domain = profile(job)
        with self._lock:
            self._reap_locked()
            if job in self._leases:
                return "同一任务已在执行"
            holder = self._domain_busy_locked(domain, job)
            if holder:
                return "写域 %s 被 %s 占用" % (domain, holder)
            if kind in (COMPUTE, FETCH):
                heavy = self._count_locked(COMPUTE) + self._count_locked(FETCH)
                if heavy >= _MAX_TOTAL:
                    running = [ls.job for ls in self._leases.values()
                               if ls.kind in (COMPUTE, FETCH)]
                    return "总闸已满（%d/%d，正在跑：%s）" % (
                        heavy, _MAX_TOTAL, ", ".join(running) or "-")
            if self._count_locked(kind) >= self._quota.get(kind, 1):
                running = [ls.job for ls in self._leases.values() if ls.kind == kind]
                return "%s 配额已满（正在跑：%s）" % (kind, ", ".join(running) or "-")
            return "未知"

    def status(self) -> dict:
        """当前闸内状态（供诊断接口 / 前端「系统状态」面板展示）。"""
        with self._lock:
            self._reap_locked()
            running = [ls.to_dict() for ls in self._leases.values()]
            by_kind: dict[str, int] = {}
            for ls in self._leases.values():
                by_kind[ls.kind] = by_kind.get(ls.kind, 0) + 1
            return {
                "quota": dict(self._quota),
                "running": running,
                "running_count": len(running),
                "by_kind": by_kind,
                # 2026-11-01：把回收判据也暴露出来 —— 排查「租约为什么不释放」时，
                # 第一眼要看的就是「心跳超时是多少、绝对上限是多少」。
                "heartbeat_timeout_sec": _HEARTBEAT_TIMEOUT,
                "abs_max_sec": _ABS_MAX,
                "rejected_total": sum(self._rejected.values()),
                "rejected_top": sorted(self._rejected.items(), key=lambda kv: -kv[1])[:8],
            }


# 全局单例
BRAKE = Brake()


# ---------------------------------------------------------------- 失败退避
# 2026-10-29 实测病灶：调度异常每 10 秒重试一次，日志 17:41:02→17:41:57 刷了 6 轮 ×6 个任务。
# 失败任务反复重入，本身就在消耗 CPU 与 DB 锁，是"越修越慢"的正反馈。
# 对策：指数退避 60s → 300s → 900s → 1800s（封顶），成功一次立即归零。
_BACKOFF_STEPS = (60, 300, 900, 1800)
_backoff_lock = threading.Lock()
_backoff_state: dict[str, dict] = {}   # job -> {fails: int, next_at: float, last_err: str}


def backoff_blocked(job: str) -> float:
    """返回还需等待的秒数；0 表示可以跑。"""
    with _backoff_lock:
        st = _backoff_state.get(job)
        if not st:
            return 0.0
        return max(0.0, st["next_at"] - time.time())


def backoff_fail(job: str, err: str = "") -> float:
    """记一次失败，返回下次允许尝试的等待秒数。"""
    with _backoff_lock:
        st = _backoff_state.setdefault(job, {"fails": 0, "next_at": 0.0, "last_err": ""})
        st["fails"] = int(st["fails"]) + 1
        wait = _BACKOFF_STEPS[min(st["fails"] - 1, len(_BACKOFF_STEPS) - 1)]
        st["next_at"] = time.time() + wait
        st["last_err"] = (err or "")[:200]
        return float(wait)


def backoff_ok(job: str) -> None:
    with _backoff_lock:
        _backoff_state.pop(job, None)


def backoff_status() -> dict:
    with _backoff_lock:
        now = time.time()
        return {
            job: {
                "fails": st["fails"],
                "wait_sec": max(0, int(st["next_at"] - now)),
                "last_err": st["last_err"],
            }
            for job, st in _backoff_state.items()
        }
