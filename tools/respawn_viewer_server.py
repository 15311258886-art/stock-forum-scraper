# -*- coding: utf-8 -*-
"""拉起查看器后端（detached + 脱离作业对象），供「改了 server.py 要生效」时安全重启。

背景：本工程的 server.py 自带单实例锁（viewer/.server.pid），且必须用项目虚拟环境
``.venv\\Scripts\\python.exe``（系统/沙箱自带的 python 缺 PyYAML 等依赖，起不来）。
本脚本读 .server.pid 安全停旧实例 → 以「独立、脱离父作业对象」的方式拉新实例 →
等端口就绪，并把结果打印出来（供排障/回归留证）。

用法：
    python tools/respawn_viewer_server.py            # 重启（先停旧、再拉新）
    python tools/respawn_viewer_server.py --keep     # 端口已在监听则不重启（只体检）
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(ROOT, "viewer", ".server.pid")
LOG_FILE = os.path.join(ROOT, "viewer", "logs", "server-standalone.log")
PORT = int(os.environ.get("VIEWER_PORT", "8765"))
EXE = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def port_open(host: str = "127.0.0.1", port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def listeners(port: int = PORT) -> list[str]:
    """占用 port 的 LISTENING 进程 PID（中文 Windows 输出按 GBK 解）。"""
    raw = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True).stdout or b""
    txt = raw.decode("gbk", "ignore")
    pids: set[str] = set()
    for ln in txt.splitlines():
        if f":{port}" in ln and "LISTENING" in ln.upper():
            pids.add(ln.split()[-1])
    return sorted(pids)


def stop_old() -> list[str]:
    """停掉 .server.pid 记录的进程 + 任何仍监听端口的进程；返回被结束的 PID。"""
    killed = []
    pids = set(listeners())
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            pid = f.read().strip().split()[0]
        if pid.isdigit():
            pids.add(pid)
    except Exception:  # noqa: BLE001
        pass
    for p in sorted(pids):
        r = subprocess.run(["taskkill", "/PID", p, "/F"], capture_output=True)
        killed.append(p)
        print("  taskkill %s -> %s" % (p, ((r.stdout or b"") + (r.stderr or b"")).decode("gbk", "ignore").strip()[:100]))
    for _ in range(30):
        if not port_open():
            break
        time.sleep(0.3)
    return killed


def spawn() -> int:
    """以 detached + breakaway 方式拉新实例；返回 pid。"""
    if not os.path.exists(EXE):
        raise SystemExit("虚拟环境 python 不存在：%s" % EXE)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    fh = open(LOG_FILE, "ab", buffering=0)      # 不随 with 关闭；交给子进程长期持有
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    try:
        proc = subprocess.Popen(
            [EXE, "-u", "-m", "viewer.server"], cwd=ROOT,
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=flags, close_fds=True)
    except OSError:
        # 作业对象不允许 breakaway 时退回（仍 detached）
        proc = subprocess.Popen(
            [EXE, "-u", "-m", "viewer.server"], cwd=ROOT,
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, close_fds=True)
    return proc.pid


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="安全重启查看器后端（detached）")
    ap.add_argument("--keep", action="store_true", help="端口已在监听时只体检、不重启")
    args = ap.parse_args(argv)

    print("EXE =", EXE)
    if args.keep and port_open():
        print("端口已在监听，--keep 跳过重启；listeners=%s" % listeners())
        return 0
    print("停旧实例：")
    stop_old()
    pid = spawn()
    print("已拉起新实例 pid =", pid)
    ok = False
    for _ in range(80):
        time.sleep(0.5)
        if port_open():
            ok = True
            break
    print("端口就绪：", ok, "| listeners =", listeners())
    try:
        print("pid 文件 =", open(PID_FILE, encoding="utf-8").read().strip())
    except Exception as e:  # noqa: BLE001
        print("读 pid 文件失败：", e)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
