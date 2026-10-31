# -*- coding: utf-8 -*-
"""东财熔断器回归：`mark_eastmoney_failure()` 从上线起**一次都没生效**。

## bug 的形状（2026-10-30 复现）

`viewer/cacheutil.py::mark_eastmoney_failure` 原来只写了 `global _em_limited_until`：

```python
def mark_eastmoney_failure() -> None:
    global _em_limited_until                       # ← 只声明了这一个
    now = time.time()
    with _em_limited_lock:
        _em_fail_stamps.append(now)                # 读
        _em_fail_stamps = [t for t in ...]         # 赋 → 该名被判定为**局部**
```

第 208 行的赋值让 `_em_fail_stamps` 变成局部变量 → 第 206 行读它必抛
`UnboundLocalError: cannot access local variable '_em_fail_stamps' where it is
not associated with a value`。后果：`eastmoney_suppressed()` 永远 `False`（熔断从未生效），
且唯一调用点（`viewer/server.py::_em_get_json`）当时**没有 try/except**，东财一失败就向上抛，
把注释里承诺的"网络失败时不反查、直接交给腾讯兜底"整条兜底路径堵死。

## 本文件的判据

1. 连调 6 次 → `eastmoney_suppressed()` 为 True（`_EM_FAIL_THRESHOLD=6`）；
2. 5 次**不**抑制（确认阈值语义，不是"两三次就熔断"）；
3. 抑制窗口（`_EM_SUPPRESS_SECONDS=60`）过后自动恢复；
4. 第 201 行那个 `UnboundLocalError` 作为**显式回归点**：函数里不许再出现它；
5. 结构护栏：`_em_fail_stamps` 在函数字节码里必须是 `LOAD_GLOBAL`（不是 `LOAD_FAST`）；
6. `_em_get_json` 侧：熔断记账抛异常**不得**打断取数主流程（返回 None 而不是抛）。
"""
from __future__ import annotations

import dis
import logging
import sys
import time as _real_time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _detach_prod_log_handlers() -> list:
    """摘掉根 logger 上的 FileHandler（同 `tests/test_offproc_backoff_retry.py` 的说明）。

    `viewer/server.py` 导入时会把 `viewer/logs/server-<date>.log` 挂到根 logger，
    测试里的 `logging.*` 会写进生产日志。本文件导入了 `viewer.server`，故一并摘掉。
    """
    root = logging.getLogger()
    removed = [h for h in list(root.handlers) if isinstance(h, logging.FileHandler)]
    for h in removed:
        root.removeHandler(h)
    return removed


class _FakeClock:
    """可控时钟：只提供 `time()`/`monotonic()`，够 cacheutil 用。"""

    def __init__(self, start: float = 1_000_000.0):
        self.t = float(start)

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


@pytest.fixture()
def cu(monkeypatch):
    """隔离的 cacheutil：可控时钟 + 进入前重置熔断状态，退出后再重置一次。"""
    from viewer import cacheutil as mod

    clock = _FakeClock()
    monkeypatch.setattr(mod, "time", types.SimpleNamespace(
        time=clock.time, monotonic=clock.monotonic))
    mod.reset_eastmoney_state()
    yield mod, clock
    mod.reset_eastmoney_state()


# ---------------------------------------------------------------- 1. 核心：6 次失败必须熔断
def test_six_failures_trigger_suppression(cu):
    """连调 `mark_eastmoney_failure()` 6 次 → 进入抑制。

    修复前这条必红：第 1 次调用就抛 UnboundLocalError，`eastmoney_suppressed()` 恒 False。
    """
    mod, _clock = cu
    assert mod.eastmoney_suppressed() is False
    for i in range(6):
        mod.mark_eastmoney_failure()          # 修复前：第 1 次就在这里抛 UnboundLocalError
        if i < 5:
            assert mod.eastmoney_suppressed() is False, (
                "第 %d 次失败就熔断了，阈值语义不对" % (i + 1))
    assert mod.eastmoney_suppressed() is True, (
        "6 次失败仍未进入抑制 —— 熔断器没生效（历史事故的确切症状）")


# ---------------------------------------------------------------- 2. 阈值语义
def test_five_failures_do_not_trigger_suppression(cu):
    """5 次**不**抑制（`_EM_FAIL_THRESHOLD = 6`）。"""
    mod, _clock = cu
    for _ in range(5):
        mod.mark_eastmoney_failure()
    assert mod.eastmoney_suppressed() is False


# ---------------------------------------------------------------- 3. 窗口自动恢复
def test_suppression_window_expires(cu):
    """抑制窗口 60s 过后自动恢复；窗口内一直抑制。"""
    mod, clock = cu
    for _ in range(6):
        mod.mark_eastmoney_failure()
    assert mod.eastmoney_suppressed() is True

    clock.advance(mod._EM_SUPPRESS_SECONDS - 1)
    assert mod.eastmoney_suppressed() is True, "窗口内不该提前恢复"

    clock.advance(2)
    assert mod.eastmoney_suppressed() is False, (
        "抑制窗口（_EM_SUPPRESS_SECONDS=%.0fs）过后必须自动恢复，否则东财被永久跳过"
        % mod._EM_SUPPRESS_SECONDS)


def test_failure_window_drops_stale_stamps(cu):
    """滑动窗口：30s 窗口外的失败时间戳必须被丢掉，否则历史失败会永久累计到熔断。"""
    mod, clock = cu
    for _ in range(5):
        mod.mark_eastmoney_failure()
    clock.advance(mod._EM_FAIL_WINDOW + 1)     # 5 条全部过期
    mod.mark_eastmoney_failure()               # 只剩这 1 条
    assert mod.eastmoney_suppressed() is False
    assert len(mod._em_fail_stamps) == 1, "过期的失败时间戳没有被裁掉：%r" % (mod._em_fail_stamps,)


# ---------------------------------------------------------------- 4. 显式回归点：UnboundLocalError
def test_mark_failure_does_not_raise_unbound_local(cu):
    """第 201 行那个 `UnboundLocalError` 的直接回归点（不捕获它 = 修复失败）。"""
    mod, _clock = cu
    try:
        mod.mark_eastmoney_failure()
    except UnboundLocalError as exc:          # 这就是事故里的那个异常
        pytest.fail("mark_eastmoney_failure 又抛 UnboundLocalError（`global` 漏声明）：%s" % exc)


def test_success_and_query_do_not_raise(cu):
    """`mark_eastmoney_success()` / `eastmoney_suppressed()` 逐行确认：同族函数无同型缺陷。"""
    mod, _clock = cu
    mod.mark_eastmoney_success()               # 空表也不能炸（内部有 `if _em_fail_stamps:`）
    assert mod.eastmoney_suppressed() is False

    for _ in range(3):
        mod.mark_eastmoney_failure()
    assert len(mod._em_fail_stamps) == 3
    mod.mark_eastmoney_success()               # 成功一次 → 衰减一条
    assert len(mod._em_fail_stamps) == 2


# ---------------------------------------------------------------- 5. 结构护栏（字节码级）
def test_em_fail_stamps_is_a_global_not_a_local(cu):
    """`_em_fail_stamps` 必须以 `LOAD_GLOBAL` 访问；出现 `LOAD_FAST` 就是那个 bug 回来了。"""
    mod, _clock = cu
    local_loads = {ins.argval for ins in dis.get_instructions(mod.mark_eastmoney_failure)
                   if ins.opname in ("LOAD_FAST", "STORE_FAST")}
    assert "_em_fail_stamps" not in local_loads, (
        "`_em_fail_stamps` 被当成局部变量 → 读它必抛 UnboundLocalError（漏写 global）：%s"
        % sorted(local_loads))
    assert "_em_limited_until" not in local_loads


# ---------------------------------------------------------------- 6. 调用点必须兜住
def test_em_get_json_swallows_breaker_exception(monkeypatch):
    """`_em_get_json` 里熔断记账抛异常时，**必须**仍然走到「返回 None 交给腾讯兜底」。

    修复前这里裸调 → 东财一失败就向上抛 `UnboundLocalError`，兜底路径永远走不到。
    """

    class _BadCu:
        """模拟"记账坏掉"的 cacheutil（根因已修，但纵深防御仍要测）。"""

        @staticmethod
        def eastmoney_acquire(*args, **kwargs):  # noqa: ARG004
            return True

        @staticmethod
        def eastmoney_suppressed() -> bool:
            return False

        @staticmethod
        def mark_eastmoney_success() -> None:
            return None

        @staticmethod
        def mark_eastmoney_failure() -> None:
            raise UnboundLocalError("simulated breaker failure")

    from viewer import server as S

    _detach_prod_log_handlers()                                 # 不许写进生产 server-*.log
    monkeypatch.setattr(S, "cu", _BadCu())
    monkeypatch.setattr(S, "_get_text", lambda *a, **k: None)   # 不联网

    assert S._em_get_json("https://push2.eastmoney.com/x", "https://quote.eastmoney.com/") is None, (
        "熔断记账异常把取数主流程打断了（应降级为 None，让上层走腾讯兜底）")
