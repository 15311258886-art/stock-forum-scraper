# -*- coding: utf-8 -*-
"""launch_viewer（启动器「假成功」修复）的回归测试。

为什么不连真实 8765：用户可能正跑着查看器，测试必须零副作用 ——
网络（_port_open / _probe_health）与子进程（subprocess.Popen）全部 monkeypatch，
ROOT 指向 tmp_path，锁文件与状态文件都落在临时目录，不碰真实项目。

覆盖的四条（对应缺陷清单）：
  1. /api/version 回的不是本查看器 → 退出码 2，且**不**打开浏览器（旧版会开）
  2. 健康探测始终失败 → 退出码 3，不打开浏览器，状态文件 stage=fail
  3. 健康成功 → 退出码 0，浏览器只开一次，状态文件 stage=ok
  4. 锁被占用（第二个启动器）→ 退出码 0 且不开浏览器
另加两条：指纹相符不误报 WARN；拉起子进程失败 → 退出码 4。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import launch_viewer as lv

# 本机实测的 /api/version 真实形状（viewer/server.py::api_version 的返回键）
GOOD_VERSION = {
    "version": "20261031-layout3",
    "served_at": "2026-11-01 02:40:57",
    "pid": 4242,
    "build_id": "33f80a102b28",
    "started_at": "2026-11-01 00:11:54",
    "sched_task": "-",
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把 ROOT 指到临时目录，并把等待压到毫秒级；同时记录所有外部副作用。"""
    monkeypatch.setattr(lv, "ROOT", str(tmp_path))
    monkeypatch.setattr(lv, "SERVICE_WAIT_SEC", 0.4)
    monkeypatch.setattr(lv, "POLL_INTERVAL", 0.05)
    monkeypatch.setattr(lv, "HEALTH_TIMEOUT", 0.1)

    state = types.SimpleNamespace(opened=[], notes=[], spawns=[])

    def fake_open(url, *a, **k):
        state.opened.append(url)
        return True

    def fake_notify(title, body, icon=0x00000010):
        state.notes.append((title, body))

    class _FakeProc:
        pid = 9999

    def fake_popen(*a, **k):
        state.spawns.append((a, k))
        return _FakeProc()

    monkeypatch.setattr(lv.webbrowser, "open", fake_open)
    # 绝不允许真的弹消息框（会把测试挂住）
    monkeypatch.setattr(lv, "_notify", fake_notify)
    monkeypatch.setattr(lv.subprocess, "Popen", fake_popen)

    state.status_path = tmp_path / "viewer" / "data" / "launcher_status.json"
    state.lock_path = tmp_path / ".launcher.lock"
    state.tmp_root = tmp_path

    def read_status():
        return json.loads(state.status_path.read_text(encoding="utf-8"))

    state.read_status = read_status
    return state


def test_foreign_port_exits_2_without_opening_browser(env, monkeypatch):
    """端口回的不是本查看器（别的程序占着 8765）→ 退出码 2，浏览器绝不能被打开。"""
    monkeypatch.setattr(lv, "_port_open", lambda: True)
    monkeypatch.setattr(lv, "_probe_health", lambda timeout=1.0: {"hello": "world"})

    rc = lv.main()

    assert rc == lv.EXIT_PORT_FOREIGN == 2
    assert env.opened == []          # 旧版在这里会 webbrowser.open → 假成功
    assert env.spawns == []          # 也不能去拉后端抢端口
    assert env.notes, "失败必须有可见提示"
    assert "被其它程序占用" in env.notes[0][0]
    status = env.read_status()
    assert status["stage"] == "fail"
    assert status["port_owner"] == "foreign"
    assert status["port"] == 8765
    assert status["error"]


def test_health_never_up_exits_3(env, monkeypatch):
    """服务 20 秒（测试里 0.4 秒）内没通过健康校验 → 退出码 3，浏览器不能打开。"""
    monkeypatch.setattr(lv, "_port_open", lambda: False)
    monkeypatch.setattr(lv, "_probe_health", lambda timeout=1.0: None)

    rc = lv.main()

    assert rc == lv.EXIT_HEALTH_TIMEOUT == 3
    assert env.opened == []
    assert len(env.spawns) == 1, "应当尝试拉起一次后端"
    status = env.read_status()
    assert status["stage"] == "fail"
    assert status["pid"] == 9999           # 记下了被拉起却没能变健康的那个进程
    assert "超时" in status["error"]
    assert env.notes and "超时" in env.notes[0][0]


def test_healthy_service_exits_0_and_opens_browser(env, monkeypatch):
    """已有健康的本查看器 → 复用、退出码 0、浏览器只开一次、状态文件 stage=ok。"""
    monkeypatch.setattr(lv, "_port_open", lambda: True)
    monkeypatch.setattr(lv, "_probe_health", lambda timeout=1.0: dict(GOOD_VERSION))

    rc = lv.main()

    assert rc == lv.EXIT_OK == 0
    assert env.opened == ["http://127.0.0.1:8765/"]
    assert env.spawns == [], "已有服务时不应再拉一个后端"
    status = env.read_status()
    assert status["stage"] == "ok"
    assert status["port_owner"] == "viewer"
    assert status["build_id"] == GOOD_VERSION["build_id"]
    assert status["pid"] == GOOD_VERSION["pid"]
    # tmp_path 里没有 viewer/build_id.py，算不出磁盘指纹 → 记为"未知"(None)，
    # 既不当失败也不算"一致"（不猜）
    assert status["build_match"] is None
    assert status["warn"] is None


def test_stale_build_warns_but_still_succeeds(env, monkeypatch):
    """指纹不一致 = 服务是旧版本 → 记 WARN + 明确提示，但**不**判启动失败。"""
    build_id_py = env.tmp_root / "viewer" / "build_id.py"
    build_id_py.parent.mkdir(parents=True, exist_ok=True)
    build_id_py.write_text("def of_disk(root):\n    return 'deadbeef0000'\n",
                           encoding="utf-8")
    monkeypatch.setattr(lv, "_port_open", lambda: True)
    monkeypatch.setattr(lv, "_probe_health", lambda timeout=1.0: dict(GOOD_VERSION))

    rc = lv.main()

    assert rc == lv.EXIT_OK, "旧版本不该被判死，页面现在仍然可用"
    assert env.opened == ["http://127.0.0.1:8765/"]
    status = env.read_status()
    assert status["stage"] == "ok"
    assert status["build_match"] is False
    assert status["build_id"] == GOOD_VERSION["build_id"]
    assert "旧版本" in status["warn"]
    assert "重启" in status["warn"]


def test_build_id_match_records_no_warn(env, monkeypatch):
    """磁盘指纹与对端一致时不能误报"旧版本"（否则天天喊狼来了）。"""
    build_id_py = env.tmp_root / "viewer" / "build_id.py"
    build_id_py.parent.mkdir(parents=True, exist_ok=True)
    build_id_py.write_text(
        "def of_disk(root):\n    return %r\n" % GOOD_VERSION["build_id"],
        encoding="utf-8",
    )
    monkeypatch.setattr(lv, "_port_open", lambda: True)
    monkeypatch.setattr(lv, "_probe_health", lambda timeout=1.0: dict(GOOD_VERSION))

    rc = lv.main()

    assert rc == lv.EXIT_OK
    status = env.read_status()
    assert status["build_match"] is True
    assert status["warn"] is None
    assert status["stage"] == "ok"


def test_slow_but_alive_server_is_not_misjudged_as_foreign(env, monkeypatch):
    """本查看器被 GIL 卡住、1 秒内回不了 JSON → 不能误判成"别的程序占端口"。

    依据：viewer/server.py 第 41 行记着实测「/api/version 被拖到 2.4~9.8 秒」。
    这里模拟"短超时拿不到、长超时拿到"（_probe_health_slow 的取值逻辑）。
    """
    monkeypatch.setattr(lv, "_port_open", lambda: True)

    def slow_probe(timeout=1.0):
        return dict(GOOD_VERSION) if timeout > lv.HEALTH_TIMEOUT else None

    monkeypatch.setattr(lv, "_probe_health", slow_probe)

    rc = lv.main()

    assert rc == lv.EXIT_OK, "慢不等于不是我们"
    assert env.opened == ["http://127.0.0.1:8765/"]
    assert env.spawns == []
    assert env.read_status()["stage"] == "ok"


def test_second_launcher_is_noop(env, monkeypatch):
    """锁被占用时（用户连点两下）→ 直接退出 0，不再开第二个浏览器标签。"""
    fh, held = lv._acquire_lock()
    assert held is True, "第一个启动器应当拿到锁"
    try:
        # 只要走到_launch 就会打开浏览器，这里用会抛错的方式证明根本没走到
        monkeypatch.setattr(lv, "_probe_health",
                            lambda timeout=1.0: pytest.fail("第二个启动器不该去探测服务"))
        rc = lv.main()
    finally:
        lv._release_lock(fh)

    assert rc == lv.EXIT_OK
    assert env.opened == []
    assert env.spawns == []
    assert not env.status_path.exists(), "后来者不得覆盖正在跑的启动器的状态文件"


def test_unexpected_exception_is_visible_and_releases_lock(env, monkeypatch):
    """启动器内部异常也不能静默（pythonw 下未捕获异常 = 双击没反应），且必须放锁。"""
    def boom():
        raise RuntimeError("模拟启动器内部异常")

    monkeypatch.setattr(lv, "_launch", boom)

    rc = lv.main()

    assert rc == lv.EXIT_UNEXPECTED == 1
    assert env.opened == []
    assert env.notes and "未预料" in env.notes[0][0], "异常必须有可见提示"
    status = env.read_status()
    assert status["stage"] == "fail"
    assert "RuntimeError" in status["error"]
    assert "Traceback" in status["traceback"], "traceback 要落盘，否则没法定位"
    # 异常路径也必须已放锁，否则用户之后每次双击都被"已有实例在跑"挡住
    fh, held = lv._acquire_lock()
    assert held is True
    lv._release_lock(fh)


def test_spawn_failure_exits_4(env, monkeypatch):
    """解释器不见了（拉子进程直接抛错）→ 退出码 4，浏览器不能打开。"""
    monkeypatch.setattr(lv, "_port_open", lambda: False)
    monkeypatch.setattr(lv, "_probe_health", lambda timeout=1.0: None)

    def boom(*a, **k):
        raise OSError("no such file: python.exe")

    monkeypatch.setattr(lv.subprocess, "Popen", boom)

    rc = lv.main()

    assert rc == lv.EXIT_SPAWN_FAIL == 4
    assert env.opened == []
    status = env.read_status()
    assert status["stage"] == "fail"
    assert "python.exe" in status["error"]
