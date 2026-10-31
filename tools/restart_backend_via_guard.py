"""按「任务计划守护」重启本地后端（不会关掉用户的查看器窗口）。

为什么需要它 / 为什么不能自己 Popen：
    从 agent 工具链（Bash/PowerShell）派生的进程会被会话回收 —— 自己拉起的后端
    等于"用短命实例换掉长命实例"。正确做法是**杀掉旧后端**，让本机已注册的
    两条任务计划（`股票论坛查看器-常驻守护` 常驻循环 / `股票论坛查看器兜底` 每分钟）
    用磁盘上的新代码自动把它拉起来。实测 10~30s 内恢复，且属于 Task Scheduler
    持有的进程，不会被会话回收。

用法：
    .venv\\Scripts\\python.exe tools/restart_backend_via_guard.py
    .venv\\Scripts\\python.exe tools/restart_backend_via_guard.py --check "键名"
        --check 会额外等待某个接口返回里出现指定键（用于确认新代码真的加载了）

退出码：0=成功；9=守护未在时限内拉起（此时请让用户双击桌面快捷方式，不要自己拉）。
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.request

PORT = int(os.environ.get("VIEWER_PORT", "8765"))
BASE = "http://127.0.0.1:%d" % PORT
WAIT_DOWN = 40          # 等旧进程退出（秒）
WAIT_UP = 200           # 等守护拉起（秒）

for _k in list(os.environ):
    if _k.lower() in ("http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(_k, None)
urllib.request.getproxies = lambda: {}      # 本机环境有 127.0.0.1 代理，必须绕开


def get(path: str, timeout: int = 8):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, r.read()
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def main() -> int:
    want_key = ""
    if "--check" in sys.argv:
        i = sys.argv.index("--check")
        want_key = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""

    st, body = get("/api/version", 6)
    if st != 200:
        print("后端当前不可用（%s），不重启：%s" % (st, body))
        return 9
    old_pid = json.loads(body)["pid"]
    print("[1] 终止旧后端 pid=%s" % old_pid)
    try:
        os.kill(old_pid, signal.SIGTERM)      # 不用 taskkill/schtasks（schtasks 被沙箱黑名单拦）
    except Exception as e:  # noqa: BLE001
        print("    kill 失败：%s" % e)

    t0 = time.time()
    while time.time() - t0 < WAIT_DOWN and get("/api/version", 2)[0] is not None:
        time.sleep(0.5)
    print("    旧服务已停（%.1fs）" % (time.time() - t0))

    print("[2] 等任务计划守护自动拉起新代码（常驻守护每 30s 一探 / 兜底每分钟一次）")
    # 探测主接口而不是 /status —— --check 校验的键（如 source_stats / count_spec）
    # 只在 payload 里出现，用 /status 会永远等不到（2026-10-15 自己踩过）。
    probe = "/api/backtest"
    t0 = time.time()
    while time.time() - t0 < WAIT_UP:
        st, body = get(probe, 20)
        if st == 200:
            if not want_key or want_key.encode("utf-8") in (body or b""):
                print("    就绪 %.0fs" % (time.time() - t0))
                break
            print("    端口已起但接口暂未含 %r，继续等…" % want_key)
        time.sleep(3)
    else:
        print("    ✗ %ds 内未就绪 —— 守护可能失效，请让用户双击桌面快捷方式（勿自行拉起）" % WAIT_UP)
        return 9

    st, body = get("/api/version", 6)
    print("[3] 新 pid=%s ｜ version=%s" % (json.loads(body)["pid"],
                                          json.loads(body)["version"]))
    print("    提示：跨过 90s 再复查一次 pid 是否不变，才是「真常驻」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
