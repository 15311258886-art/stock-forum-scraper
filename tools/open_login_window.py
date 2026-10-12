"""打开一个受控的淘股吧登录窗口（有头 Edge + 固定 CDP 端口）。

用途：用户在这个窗口里登录淘股吧，登录后主程序通过 CDP 端口（9333）读取
该浏览器实例的登录 Cookie 并加密存库，全程不碰用户自己的浏览器、不代登录。
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
URL = "https://www.taoguba.com.cn"

# 记录 profile 与端口到一个固定位置，供「读取 cookie」阶段复用
STATE_FILE = os.path.join(tempfile.gettempdir(), "taoguba-login-state.txt")


def main() -> int:
    if not os.path.exists(EDGE):
        print(f"[错误] 找不到 Edge：{EDGE}")
        return 1

    # 全新独立 profile：避免复用残留实例（残留实例没有 CDP 端口）
    profile = tempfile.mkdtemp(prefix="taoguba-login-")

    cmd = [
        EDGE,
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        URL,
    ]
    p = subprocess.Popen(cmd)
    print(f"[启动] Edge PID={p.pid}")
    print(f"[profile] {profile}")

    # 探测 CDP 是否就绪
    ready = False
    for _ in range(30):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/json/version", timeout=2
            ) as r:
                ready = r.status == 200
            if ready:
                break
        except Exception:
            time.sleep(1)
    print(f"[CDP] {'就绪' if ready else '未就绪'} @ http://127.0.0.1:{PORT}")

    # 写状态文件（供读取阶段使用）
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(f"port={PORT}\nprofile={profile}\n")
    print(f"[状态] 已写入 {STATE_FILE}")
    return 0 if ready else 2


if __name__ == "__main__":
    sys.exit(main())
