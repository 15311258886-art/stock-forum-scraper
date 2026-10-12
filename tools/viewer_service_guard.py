# -*- coding: utf-8 -*-
"""查看器后端常驻守护（2026-10-06，王总「点了刷新也没反应」根治件）。

背景：**服务进程一旦停止，页面只会显示「加载失败」，普通点刷新/刷新按钮救不回来**
——要刷新的不是网页，是本地进程。而此前这个服务反复随场合消失：
  · 2026-10-06 之前：服务跑在 desktop_app 的**守护线程**里，WebView2 窗口一崩/一关
    就 os._exit(0)，服务跟着没了（已由 desktop_app.py 改为拉独立进程）；
  · 但凡由"某个会话/控制台/某个父进程"点火的服务，父进程一收就跟着走。
守护的做法：自己反复活着，每 INTERVAL 秒瞄一眼 8765，发现没人监听就 detached 拉一个
独立 server 进程（server.py 自带单实例锁，重复拉起会自行退出，不会抢端口）。

注册方式（Windows，用户登录时运行、无窗口、不受任何会话影响）：
    schtasks /create /tn "股票论坛查看器服务" /f /sc onlogon /rl currentuser /tr ^
      "\"F:\\...\\.venv\\Scripts\\pythonw.exe\" \"F:\\...\\tools\\viewer_service_guard.py\""

取消注册：
    schtasks /delete /tn "股票论坛查看器服务" /f

单跑（不注册、临时用）：python tools/viewer_service_guard.py
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

# 本文件在 tools/ 下：abspath → tools → 项目根，只需退**两层**（写成三层会跑到项目上级目录
# 去 `python -m viewer.server`，模块找不到 → 子进程秒退、日志还生成在错的地方，极难察觉）。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PORT = int(os.environ.get("VIEWER_PORT", "8765"))
INTERVAL = int(os.environ.get("VIEWER_GUARD_INTERVAL", "30"))   # 秒


def port_open(host: str = "127.0.0.1", port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def ensure_service() -> bool:
    """服务不在就拉一个独立进程；返回是否真的拉起了。"""
    if port_open():
        return False
    log_dir = os.path.join(ROOT, "viewer", "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = open(os.path.join(log_dir, "guard-server.log"), "ab", buffering=0)  # 不随 with 关闭
    creation = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED | NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, "-u", "-m", "viewer.server"],
        cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=creation, close_fds=True,
    )
    for _ in range(40):        # 最多等 ~20s 看它能否就绪
        time.sleep(0.5)
        if port_open():
            return True
    return False


def main() -> int:
    # stdout 若被托管方（如 pythonw 无控制台）置空，日志一律落盘，避免排障无门
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    if "--once" in sys.argv:
        # 一次性体检：检查一次、缺服务就拉起、不常驻。给任务计划做兜底触发器用
        # （主 guard 万一自己崩了，每 10 分钟的一次性体检仍能把服务叫回来）。
        ensure_service()
        return 0
    started = ensure_service()
    print(f"[guard] 启动：端口 {PORT} 初始状态={'已就绪' if port_open() else '未就绪(已发起拉起)'}", flush=True)
    while True:
        time.sleep(INTERVAL)
        try:
            if not port_open():
                ok = ensure_service()
                print(f"[guard] {time.strftime('%H:%M:%S')} 服务掉线，已{'拉起' if ok else '尝试拉起但未就绪'}", flush=True)
        except Exception as e:  # noqa: BLE001 —— 守护自己绝不能挂
            print(f"[guard] 异常（已忽略）：{e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
