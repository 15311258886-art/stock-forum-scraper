"""一键启动查看器：后台启动本地 Web 服务并可自动打开浏览器。

用法：python viewer/launch.py [--no-open]
- 纯本地个人使用，无鉴权。浏览器打开 http://127.0.0.1:8765 即可使用。
- `--no-open`：只起服务、不自动开浏览器（排查/后台运行时用，避免反复弹标签页）。
- 关闭：在运行此脚本的终端按 Ctrl+C。

2026-09-22 修正：原先**无条件**创建 ThreadingHTTPServer，在端口已被占用时
（桌面版正在跑，或误触发两次）Windows 的 SO_REUSEADDR 会让新旧服务同时监听同一端口，
请求随机命中旧代码，表现为「改了代码却不生效」。现在先探测端口：
已在监听则只复用、不再重复 bind，避免多实例抢同一端口。
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer import server  # noqa: E402


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


if __name__ == "__main__":
    no_open = "--no-open" in sys.argv
    url = f"http://127.0.0.1:{server.PORT}"

    srv = None
    if _port_in_use("127.0.0.1", server.PORT):
        print(f"[查看器] 端口 {server.PORT} 已有服务在运行，直接复用（未重复启动）。")
    else:
        srv = server.ThreadingHTTPServer(("127.0.0.1", server.PORT), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"[查看器] 已启动：{url}")

    if no_open:
        print(f"[查看器] 已按 --no-open 跳过自动打开浏览器，请手动访问：{url}")
    else:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            print("[查看器] 未能自动打开浏览器，请手动访问：" + url)

    print("[查看器] 按 Ctrl+C 停止。")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        if srv is not None:
            srv.shutdown()
        print("\n[查看器] 已停止。")
