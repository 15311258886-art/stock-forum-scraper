# -*- coding: utf-8 -*-
"""`kline_daily` 退出码回归：`main()` 曾经恒返回 None → 失败被记成「今日已完成」。

## bug 的形状（2026-10-30 定位）

`tools/build_kline_db.py::main()` 没有 `return`（隐式 `None`），而
`tools/sched_job.py::main()` 的失败判定是：

```python
r = JOBS[name]()
if isinstance(r, dict) and r.get("ok") is False:
    return 1        # ← r 是 None 时永不成立
```

于是这个任务**退出码恒 0**，父进程（`viewer/server.py::_spawn_offproc`）据此记成功、
写当日完成标记、当天不再重试。实测日志证据（`viewer/logs/sched-kline_daily.log`）：

```
[kline_daily] ok 840.4s None        ← 840 秒，但没有任何成功/失败信号
[kline_daily] ok 1139.2s None       ← 1139 秒，同样没有信号
```

其中 1139.2s 那一轮的明细是 `完成：ok=5042 skip=3 fail=534`（9.57% 的票没取到 K 线，
`no_kline` 满屏）—— 真劣化，却被报成 ok。

## 判据

1. 健康一轮 → `ok=True` 且带可诊断字段；
2. `too_short`（次新股不足 30 根，结构性、不可修）**不**判失败；
3. 硬失败（`no_kline` / `err:*`）超过池子 2% → `ok=False`；
4. 硬失败在允许范围内 → `ok=True`（不因偶发几只网络抖动整体判失败）；
5. 候选池为空 → `ok=False`（最典型的静默失败）；
6. 契约链：`tools/sched_job.py` 确实把 `{"ok": False}` 映射成 rc=1。

全程跑在 tmp 库副本上：`DATA`/`DB_PATH`/`ADJ_PATH` 全指到 `tmp_path`，
`_fetch_one` 与 `screener` 都被打桩，**不联网、不碰生产库、不写 data/kline_daily.db**。
"""
from __future__ import annotations

import importlib.util

import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BAR = ["2026-10-30", 10.0, 11.0, 12.0, 9.5, 1000.0]


def _codes(n: int) -> list[str]:
    """生成 n 个形如 600000/000001 的合法 6 位代码。"""
    return ["%06d" % (i + 1) for i in range(n)]


@pytest.fixture()
def bd(monkeypatch, tmp_path):
    """build_kline_db 本地化：库/复权缓存都落 tmp，代码池与取数全部打桩。"""
    import tools.build_kline_db as mod   # noqa: PLC0415

    monkeypatch.setattr(mod, "DATA", str(tmp_path))
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "kline_daily.db"))
    monkeypatch.setattr(mod, "ADJ_PATH", str(tmp_path / "adjust_cache.json"))
    monkeypatch.setattr(sys, "argv", ["build_kline_db.py"])   # 无 --full / --codes
    return mod


def _run(bd, monkeypatch, pool, outcomes):
    """跑一轮 `main()`：`outcomes` 是 {code: 状态}，未列出的 code 视为 ok。

    状态取值与真实 `_fetch_one` 一致：`"ok"` / `"too_short"` / `"no_kline"` / `"err:KeyError"`。
    """
    fake_screener = types.ModuleType("screener")
    fake_screener._market_pool = lambda: list(pool)
    monkeypatch.setitem(sys.modules, "screener", fake_screener)

    def fake_fetch_one(code):
        state = outcomes.get(code, "ok")
        if state in ("ok", "ok_noex"):
            return code, [list(BAR)], [], "ok"
        return code, None, [], state

    monkeypatch.setattr(bd, "_fetch_one", fake_fetch_one)
    return bd.main()


# ---------------------------------------------------------------- 1. 健康一轮
def test_healthy_run_returns_ok_true_with_diagnostics(bd, monkeypatch):
    res = _run(bd, monkeypatch, _codes(50), {})
    assert isinstance(res, dict), "main() 必须返回 dict（返回 None 就是那个 bug 本身）"
    assert res["ok"] is True
    assert res["codes"] == 50
    assert res["fail"] == 0
    assert res["hard_fail"] == 0
    assert res["rows"] == 50
    assert res["err"] is None
    assert res["elapsed"] >= 0


# ---------------------------------------------------------------- 2. 软失败不判失败
def test_too_short_is_structural_and_does_not_fail(bd, monkeypatch):
    """次新股不足 30 根 K 线是结构性的（实测正常轮 16~31 只），不能算劣化。"""
    pool = _codes(100)
    outcomes = {c: "too_short" for c in pool[:20]}
    res = _run(bd, monkeypatch, pool, outcomes)
    assert res["soft_fail_too_short"] == 20
    assert res["hard_fail"] == 0
    assert res["ok"] is True, "20 只 too_short 就把整轮判失败 → 本任务永远失败、天天重试"


# ---------------------------------------------------------------- 3. 硬失败超阈值判失败
def test_hard_failures_above_ratio_fail_the_run(bd, monkeypatch):
    """10/100 = 10% 硬失败（实测 534/5579=9.57% 那种劣化）→ 必须 ok=False。"""
    pool = _codes(100)
    outcomes = {c: "no_kline" for c in pool[:10]}
    res = _run(bd, monkeypatch, pool, outcomes)
    assert res["hard_fail"] == 10
    assert res["allowed_hard_fail"] == 3        # max(3, int(100*0.02))
    assert res["ok"] is False, "劣化轮被判成成功 → 父进程写当日完成标记 → 当天不再重试"
    assert "hard" in (res["err"] or "") or "硬失败" in (res["err"] or "")


def test_err_state_counts_as_hard_failure(bd, monkeypatch):
    """`err:*`（解析/代码异常）同样算硬失败。"""
    pool = _codes(100)
    outcomes = {c: "err:KeyError" for c in pool[:10]}
    res = _run(bd, monkeypatch, pool, outcomes)
    assert res["hard_fail"] == 10
    assert res["ok"] is False


# ---------------------------------------------------------------- 4. 允许范围内的硬失败放行
def test_hard_failures_within_allowance_pass(bd, monkeypatch):
    """3/100 硬失败在允许范围内（不因偶发几只网络抖动整体判失败）。"""
    pool = _codes(100)
    outcomes = {c: "no_kline" for c in pool[:3]}
    res = _run(bd, monkeypatch, pool, outcomes)
    assert res["hard_fail"] == 3
    assert res["allowed_hard_fail"] == 3
    assert res["ok"] is True


def test_ratio_boundary_is_inclusive(bd, monkeypatch):
    """边界：恰好等于允许值 → 放行；多 1 只 → 失败。"""
    pool = _codes(1000)                       # allowed = max(3, 20) = 20
    res = _run(bd, monkeypatch, pool, {c: "no_kline" for c in pool[:20]})
    assert res["allowed_hard_fail"] == 20
    assert res["ok"] is True

    res = _run(bd, monkeypatch, _codes(1000), {c: "no_kline" for c in _codes(1000)[:21]})
    assert res["ok"] is False


# ---------------------------------------------------------------- 5. 空池必须失败
def test_empty_pool_fails_instead_of_silently_succeeding(bd, monkeypatch):
    """候选池为空 → ok=False。以前会「跑 0 只、rc=0」，是最典型的静默失败。"""
    res = _run(bd, monkeypatch, [], {})
    assert res["ok"] is False
    assert res["codes"] == 0
    assert "候选池" in res["err"]


# ---------------------------------------------------------------- 6. 契约链
@pytest.fixture()
def sched_job():
    path = ROOT / "tools" / "sched_job.py"
    spec = importlib.util.spec_from_file_location("sched_job_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sched_job_maps_ok_false_to_rc1(sched_job, monkeypatch, tmp_path):
    """`tools/sched_job.py` 的退出码判定：只有拿到带 `ok` 的 dict 才有信息量。

    2026-10-31 整改（QA 挖出的真缺陷）：`main()` 的 finally 里会写一行 job_runs，
    而此前 `viewer/health.py::APP_DB` 硬编码生产库 → 本用例每跑一次就往**生产**
    日更健康表塞 3 行假记录（ok=0 / err='x'，累积 18 条，差点被误判成 kline_daily 真故障）。
    现在把写入重定向到 pytest 的 tmp_path，并**断言临时库确实收到 3 行** ——
    证明是「隔离了写入」而不是「把写入弄丢了」。
    """
    from viewer import health as H
    iso_db = tmp_path / "app_iso.db"
    monkeypatch.setattr(H, "APP_DB", str(iso_db))

    monkeypatch.setattr(sys, "argv", ["sched_job.py", "kline_daily"])
    monkeypatch.setattr(sched_job, "JOBS", {"kline_daily": lambda: {"ok": False, "err": "x"}})
    assert sched_job.main() == 1

    monkeypatch.setattr(sched_job, "JOBS", {"kline_daily": lambda: {"ok": True}})
    assert sched_job.main() == 0

    # 修复前的形态：main() 返回 None → 永远 rc=0（这就是 11 天静默失败的机制）
    monkeypatch.setattr(sched_job, "JOBS", {"kline_daily": lambda: None})
    assert sched_job.main() == 0, "None 被判成成功是历史行为，这里固化它、作为对照"

    # 写入必须落在隔离库上（3 次 main() → 3 行），生产库一行都不该多。
    con = sqlite3.connect(str(iso_db))
    try:
        rows = con.execute(
            "SELECT job, ok, rc FROM job_runs ORDER BY id").fetchall()
    finally:
        con.close()
    assert [r[1] for r in rows] == [0, 1, 1], "ok 列应与三次调用的结论一致（False/True/None）"
    assert all(r[0] == "kline_daily" for r in rows)


def test_kline_daily_job_wrapper_returns_the_dict(sched_job, bd, monkeypatch):
    """`tools/sched_job.py::_kline_daily` 必须把 main() 的返回值原样交出去（没被吞掉）。"""
    res = _run(bd, monkeypatch, _codes(10), {})
    # `_kline_daily` 里是 `import build_kline_db`（生产环境靠 `python tools/sched_job.py`
    # 把 tools/ 放进 sys.path[0]）。测试里直接把这个名字指向已加载的模块，效果相同。
    monkeypatch.setitem(sys.modules, "build_kline_db", bd)
    monkeypatch.setattr(bd, "main", lambda: res)
    monkeypatch.setattr(sys, "argv", ["sched_job.py", "kline_daily"])
    out = sched_job._kline_daily()
    assert isinstance(out, dict) and out["ok"] is True
