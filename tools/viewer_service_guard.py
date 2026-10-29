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

# 2026-10-29 修复（「服务明明在跑，却被反复拉起新进程」的真凶）：
#   旧实现只有**一次 0.6s 超时**的 connect 探测，失败即判定「服务掉线」并 detached 拉起新
#   server。但后端是单进程多线程 + GIL：一旦某个面板在算重活（回测/K 线物化/量化因子），
#   accept 线程可能几十毫秒到几秒拿不到 GIL，监听队列一满，connect 就在 0.6s 内超时 ——
#   于是「服务很忙」被误判成「服务死了」。
#   实测后果（server-20261029.log）：每分钟出现一次「已启动」，端口上出现跑着**不同 build**
#   的新进程，旧进程被接管终止 → 用户正在看的请求被腰斩，表现就是「打开后一直抓取不到、
#   刷新也没用」。更糟的是每次拉起的子进程都要 import 一个 1.3 万行的 server 模块（约 2s
#   纯 CPU），全落在本已吃紧的 GIL 上，形成自我放大的雪崩。
#   修法：① 单次探测超时 0.6s → 2.0s；② 必须**连续 3 次**探测失败（间隔 1s）才判死；
#        ③ 拉起动作加**闸门文件**，常驻守护与「兜底 --once」不会同时拉起两个。
PROBE_TIMEOUT = float(os.environ.get("VIEWER_GUARD_PROBE_TIMEOUT", "2.0"))
PROBE_TRIES = int(os.environ.get("VIEWER_GUARD_PROBE_TRIES", "3"))
SPAWN_GATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "viewer", "logs", ".guard_spawn_gate")
SPAWN_GATE_TTL = 90.0        # 闸门有效期：超过这个时间没刷新，视为上一次拉起已结束


def port_open(host: str = "127.0.0.1", port: int = PORT,
              timeout: float | None = None) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(PROBE_TIMEOUT if timeout is None else timeout)
        return s.connect_ex((host, port)) == 0


def service_alive() -> bool:
    """**连续 PROBE_TRIES 次都连不上**才算掉线（单次超时只说明它忙，不说明它死）。"""
    for i in range(max(1, PROBE_TRIES)):
        if port_open():
            return True
        if i + 1 < PROBE_TRIES:
            time.sleep(1.0)
    return False


def _gate_held() -> bool:
    """闸门是否被另一个 guard 占着（防止 30s 常驻 + 每分钟兜底 同时拉起两个 server）。"""
    try:
        st = os.stat(SPAWN_GATE)
        if time.time() - st.st_mtime > SPAWN_GATE_TTL:
            return False
        try:
            pid = int(open(SPAWN_GATE, encoding="utf-8").read().strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and pid == os.getpid():
            return True
        # 对方进程还在（Windows 上 os.kill(pid,0) 不可用，用 OpenProcess 判定）
        return bool(pid) and _pid_alive(pid)
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == 259                 # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return False


def ensure_service() -> bool:
    """服务不在就拉一个独立进程；返回是否真的拉起了。"""
    if service_alive():
        return False
    if _gate_held():
        return False
    log_dir = os.path.join(ROOT, "viewer", "logs")
    os.makedirs(log_dir, exist_ok=True)
    # 抢占闸门：**原子**创建（O_CREAT|O_EXCL），90s 内其它 guard 看到就不重复拉起。
    # 2026-10-29 修（重复进程的根源）：原先是「先 open(...,"w") 再写 pid」，
    # 检查(_gate_held) 与写入之间有明显竞态窗口 —— 两个 guard 在同一瞬间都探到服务掉线，
    # 就**双双拉起后端**。实测一次启动出现两个 viewer.server 进程：一个抢到 8765，
    # 另一个把模块导入、调度线程、启动预热全跑完才发现端口被占而退出 ——
    # 白占一份内存、重复抢 GIL，用户感知就是「刚打开界面一堆面板加载失败」。
    # 改用 O_EXCL 原子创建：同一时刻只有一个能建成，另一个立即让路。
    try:
        _fd = os.open(SPAWN_GATE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(_fd, str(os.getpid()).encode())
        os.close(_fd)
    except FileExistsError:
        if _gate_held():
            return False                     # 对方正在拉起，让路
        try:                                 # 僵尸闸门（持有者已死）→ 清掉重抢一次
            os.remove(SPAWN_GATE)
            _fd = os.open(SPAWN_GATE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(_fd, str(os.getpid()).encode())
            os.close(_fd)
        except OSError:
            return False
    except OSError:
        pass
    fh = open(os.path.join(log_dir, "guard-server.log"), "ab", buffering=0)  # 不随 with 关闭
    creation = 0x00000008 | 0x00000200 if os.name == "nt" else 0  # DETACHED | NEW_PROCESS_GROUP
    print(f"[guard] {time.strftime('%H:%M:%S')} 连续 {PROBE_TRIES} 次探测失败，拉起后端…", flush=True)
    subprocess.Popen(
        [sys.executable, "-u", "-m", "viewer.server"],
        cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=creation, close_fds=True,
    )
    # 冷启动实测 ~3s（解释器 0.3s + 模块导入 1.8s + 阶段 ~0.5s），首次会慢些；
    # 给 30s，别在它还没 bind 完就放弃（旧值 20s 在磁盘被占时偏紧）。
    for _ in range(60):
        time.sleep(0.5)
        if port_open():
            try:
                os.remove(SPAWN_GATE)    # 拉起来了，闸门立即释放，别挡住下一次真掉线
            except OSError:
                pass
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
    print(f"[guard] 启动：端口 {PORT} 初始状态={'已就绪' if service_alive() else '未就绪(已发起拉起)'}", flush=True)
    while True:
        time.sleep(INTERVAL)
        try:
            if not service_alive():
                ok = ensure_service()
                print(f"[guard] {time.strftime('%H:%M:%S')} 服务掉线，已{'拉起' if ok else '尝试拉起但未就绪'}", flush=True)
        except Exception as e:  # noqa: BLE001 —— 守护自己绝不能挂
            print(f"[guard] 异常（已忽略）：{e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
