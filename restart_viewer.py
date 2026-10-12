# -*- coding: utf-8 -*-
"""重启查看器：结束占用 8765 的旧 python 进程 → 拉新的 desktop_app → 验证。

背景（2026-09-25 20:20）：用户截图显示「游资龙虎榜」报 HTTP 404 /api/lhb。
经查 8765 被 PID 31112 占用（启动于 11:12），而 server.py 20:05、app.js 20:16 才改完
——即运行中的是旧代码。用户明确要求不要让他反复找文件/自己点，故由助手直接重启。

安全约束：
- 只结束「监听 127.0.0.1:8765 的 python.exe」，不做通配杀进程。
- 新实例以 DETACHED_PROCESS 启动，脱离本会话，父进程退出不影响窗口。
- 启动后校验 8765 重新监听 + /api/lhb 返回 200，失败则原样报出，不静默。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
# 用「运行本脚本的同一个 Python」拉起 desktop_app——避免硬编码 .venv 路径在 venv 不存在时失效。
# 即：你用什么 Python 双击/运行 restart_viewer.py，就用什么 Python 起查看器（依赖一致）。
PY = sys.executable
ENTRY = os.path.join(BASE, "viewer", "desktop_app.py")
LOG = os.path.join(BASE, "viewer_restart.log")
PORT = 8765


def _port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _owner_pids(port: int) -> list[str]:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="ignore").stdout
    except Exception:
        return []
    pids = []
    for ln in out.splitlines():
        if f":{port}" in ln and "LISTENING" in ln:
            pids.append(ln.split()[-1])
    return sorted(set(pids))


def _pid_name(pid: str) -> str:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="ignore").stdout.strip()
        return out.split(",")[0].strip('"') if out else ""
    except Exception:
        return ""


def main() -> int:
    print(f"[restart] 目标端口 {PORT}，当前监听: {_port_open(PORT)}")
    pids = _owner_pids(PORT)
    print(f"[restart] 占用进程: {pids}")
    for pid in pids:
        nm = _pid_name(pid)
        print(f"[restart] PID {pid} -> {nm}")
        if "python" not in nm.lower():
            print(f"[restart] 跳过非 python 进程 {pid}({nm})，不擅自杀")
            continue
        r = subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True,
                           text=True, encoding="utf-8", errors="ignore")
        print(f"[restart] taskkill {pid} rc={r.returncode} {r.stdout.strip()[:80]}")
    time.sleep(2)
    print(f"[restart] 结束旧实例后端口监听: {_port_open(PORT)}")

    if not os.path.exists(PY):
        print(f"[restart] 失败：找不到解释器 {PY}")
        return 1

    DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    logf = open(LOG, "w", encoding="utf-8")
    p = subprocess.Popen([PY, ENTRY], cwd=BASE, stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=DETACHED, close_fds=True)
    print(f"[restart] 已拉起 desktop_app.py，PID={p.pid}，日志 -> {os.path.basename(LOG)}")

    ok = False
    for i in range(20):
        time.sleep(1.5)
        if _port_open(PORT):
            ok = True
            print(f"[restart] 端口 {PORT} 已就绪（等待 {(i + 1) * 1.5:.1f}s）")
            break
    if not ok:
        print("[restart] 失败：等待 30s 端口仍未监听，日志尾部：")
        try:
            with open(LOG, encoding="utf-8", errors="ignore") as f:
                for ln in f.read().splitlines()[-15:]:
                    print("   ", ln)
        except Exception as e:
            print("    读日志失败", e)
        return 1

    time.sleep(2)
    for path in ["/api/lhb", "/api/players", "/api/summary",
                 "/api/fund_strategies?key=cci_zero_cross"]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=40) as r:
                body = r.read().decode("utf-8", "ignore")
            extra = ""
            if path == "/api/lhb":
                j = json.loads(body)
                extra = f"  dates={len(j.get('dates') or [])} stocks={len(j.get('stocks') or [])} seats={len(j.get('seats') or [])}"
            elif path.startswith("/api/fund_strategies"):
                j = json.loads(body)
                extra = (f"  available={j.get('available')} funds={j.get('fund_count')} "
                         f"key={(j.get('strategy') or {}).get('key')}")
            print(f"[restart] {path} -> HTTP {r.status} len={len(body)}{extra}")
        except Exception as e:
            print(f"[restart] {path} -> 异常 {type(e).__name__} {str(e)[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
