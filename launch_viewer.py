# -*- coding: utf-8 -*-
"""浏览器模式启动器（绕过 WebView2 原生窗口崩溃）。

双击桌面 .lnk 时由**用户会话**调用（非 agent 会话），因此拉起的纯后端
进程独立于 WorkBuddy，关机前持续运行。

逻辑：
  - 探测 127.0.0.1:8765；未监听则 DETACHED 拉起纯后端（不依赖 WebView2）；
  - 端口就绪后打开默认浏览器访问查看器首页。
2026-09-28：原 desktop_app 原生窗口因本机 WebView2 运行时存在 153/154 双版本
冲突、渲染进程启动即崩，导致「双击没反应」。改为浏览器模式作为稳定入口。
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
PYV = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
LOG = os.path.join(ROOT, "server_bg.log")


def _port_open() -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


def main() -> None:
    if not _port_open():
        # DETACHED + NEW_PROCESS_GROUP：脱离 .lnk 启动进程，独立常驻
        subprocess.Popen(
            [PYV, "-m", "viewer.server"],
            cwd=ROOT,
            creationflags=0x00000008 | 0x00000200,
            stdout=open(LOG, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
    for _ in range(40):
        if _port_open():
            break
        time.sleep(0.5)
    webbrowser.open(f"http://127.0.0.1:{PORT}/")


if __name__ == "__main__":
    main()
