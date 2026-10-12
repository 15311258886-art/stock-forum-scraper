"""接管用户现有 Edge：杀进程 → 用同一 profile + --remote-debugging-port=9333 重启。

为什么这样做：
- capture_login.py 之前用 `tempfile.mkdtemp()` 开独立 profile 的 Edge，与用户的 Edge 是隔离的 → 永远读不到用户已经登录的 cookie。
- Edge 同时只能有一个实例占用同一个 user-data-dir。所以接管流程必须先杀、再起。

注意：
- Edge 重启时 Windows 会恢复关闭前的标签页（如果用户没在 Edge 设置里关掉该选项）
- 登录态保持在 user profile 里（SessionStorage 除外，但 cookie 持久化）→ 杀进程不会丢登录态
- 重启时 Edge 仍会启动「上次未关闭的标签页」——用户标签页不会丢
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
PORT = 9333
PROFILE = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")


def find_edge() -> str | None:
    """在标准安装位置查找 msedge.exe"""
    for cand in EDGE_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return None


# 旧变量保留（向后兼容）
EDGE = find_edge() or EDGE_CANDIDATES[0]


def _stdout(s):
    sys.stdout.write(s + "\n")
    sys.stdout.flush()


def _is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket()
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_port_listening(port: int, timeout: float = 25.0) -> bool:
    """等待端口开始接收连接（进入，等待直到端口开放或超时）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_port_free(port):
            return True
        time.sleep(0.5)
    return False


def list_existing_edge() -> list[dict]:
    """列出当前所有 msedge.exe 进程的 PID + 内存占用"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return []
    rows = []
    for ln in out.strip().splitlines():
        parts = [p.strip().strip('"') for p in ln.split(",")]
        if len(parts) >= 5 and parts[0].lower() == "msedge.exe":
            try:
                rows.append({"pid": int(parts[1]), "mem_kb": int(parts[4].replace(".", "").replace(",", "") or 0)})
            except Exception:
                rows.append({"pid": int(parts[1])})
    return rows


def kill_all_edge(timeout: float = 8.0) -> int:
    """杀所有 msedge.exe 进程。返回实际杀掉的进程数。"""
    rows = list_existing_edge()
    n0 = len(rows)
    if n0 == 0:
        return 0
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "msedge.exe", "/T"],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception as e:
        _stdout(f"[warn] taskkill 失败：{e}")
    # 等进程退出
    for _ in range(int(timeout * 2)):
        if not list_existing_edge():
            break
        time.sleep(0.5)
    return n0


def launch_edge_with_debug(profile: str = PROFILE, port: int = PORT) -> subprocess.Popen | None:
    """启动 Edge 附加调试端口与指定 user-data-dir"""
    if not os.path.exists(EDGE):
        _stdout(f"[错误] 找不到 Edge：{EDGE}")
        return None
    if not os.path.isdir(profile):
        _stdout(f"[错误] 找不到 Edge profile：{profile}")
        return None
    args = [
        EDGE,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # 启动时同时打开 tgb.cn 登录引导页，用户一眼能看到该做什么
        "https://www.tgb.cn",
    ]
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=0x00000008 if os.name == "nt" else 0,  # DETACHED_PROCESS
        )
        return proc
    except Exception as e:
        _stdout(f"[错误] 启动 Edge 失败：{e}")
        return None


def wait_cdp_ready(port: int = PORT, timeout: float = 25.0) -> bool:
    """用 CDP /json/version 探测 Edge 调试端口是否就绪"""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                v = json.loads(r.read().decode())
            _stdout(f"[CDP] 已就绪：{v.get('Browser', 'edge')}")
            return True
        except Exception:
            time.sleep(0.6)
    return False


def main() -> int:
    out = {
        "ok": False,
        "profile": PROFILE,
        "edge_path": EDGE,
        "port": PORT,
        "killed": 0,
        "launched": False,
        "cdp_ready": False,
    }
    if not os.path.exists(EDGE):
        out["error"] = f"找不到 Edge：{EDGE}"
        print(json.dumps(out, ensure_ascii=False))
        return 1
    if not os.path.isdir(PROFILE):
        out["error"] = f"找不到用户 profile：{PROFILE}"
        print(json.dumps(out, ensure_ascii=False))
        return 2

    _stdout(f"[profile] {PROFILE}")
    out["killed"] = kill_all_edge()
    _stdout(f"[已杀] {out['killed']} 个 msedge.exe 进程")

    proc = launch_edge_with_debug(PROFILE, PORT)
    out["launched"] = proc is not None
    if not out["launched"]:
        out["error"] = "启动 Edge 失败"
        print(json.dumps(out, ensure_ascii=False))
        return 3

    out["cdp_ready"] = wait_cdp_ready(PORT)
    out["ok"] = out["cdp_ready"]
    if not out["cdp_ready"]:
        out["error"] = "Edge 已启动但 CDP 端口 25 秒内未就绪"
        print(json.dumps(out, ensure_ascii=False))
        return 4

    out["pid"] = proc.pid
    out["message"] = (
        "Edge 已用您的 profile 重新启动并启用调试端口。"
        "现在到新开的 Edge 里登录淘股吧（扫码 / 短信），"
        "capture_login.py 会自动捕获 cookie 存库。"
    )
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())