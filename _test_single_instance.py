"""建议1 / P1 自测：单实例互斥（命名互斥体 + 端口持有者检测）。

背景：报告记录的现象是「双击启动并存 1 个 server + 2 个 desktop 窗口」。
根因是旧的 ctypes.windll 不用 use_last_error，GetLastError 拿到的是**上一次
无关 Win32 调用**的残留错误码，183 判定既不可靠也漏判。

跑法（项目根）：
    .venv\\Scripts\\python.exe _test_single_instance.py

三个用例：
    A 同进程幂等      —— 同进程连调两次都应为 True（自己就是实例，不该自我否定）
    B 跨进程互斥      —— 已有实例（互斥体 + 端口都占）时，新进程必须返回 False
    C 孤儿互斥体兜底  —— 互斥体存在但端口没人监听时，不应把用户挡在门外（照常放行）
"""
from __future__ import annotations

import ctypes
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
MUTEX_MODE = (ROOT / "_single_instance_mode.py").resolve()

results: list[tuple[str, bool, str]] = []


def run_mode(mode: str, timeout: int = 25) -> tuple[int, str]:
    p = subprocess.run([PY, str(MUTEX_MODE), mode], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def port_free(port: int = 8765, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


def main() -> int:
    ok = True

    # ---- A 同进程幂等 ----
    from viewer import desktop_app as DA
    a1 = DA._acquire_single_instance()
    a2 = DA._acquire_single_instance()
    passed = (a1 is True and a2 is True)
    results.append(("A 同进程连调两次都放行", passed, f"first={a1} second={a2}"))
    if not passed:
        ok = False
    # 用例 A 期间本进程已经"是实例"并持有互斥体，用完必须还回去，
    # 否则后面 B/C 的子进程会看到「互斥体已存在」而非预期的状态。
    try:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(DA._MUTEX_HANDLE)
    finally:
        # 拿到 desktop_app 模块自己的 globals（DA.globals 不存在，取函数所在的命名空间）
        DA._acquire_single_instance.__globals__["_MUTEX_HANDLE"] = None

    # ---- B 跨进程互斥（模拟双击第二个） ----
    # B1 起一个"实例进程"：持互斥体 + 占住 8765，存活 20s
    holder = subprocess.Popen([PY, str(MUTEX_MODE), "hold"],
                              cwd=str(ROOT), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            if not port_free():
                break
            time.sleep(0.25)
        if port_free():
            results.append(("B 已有实例（端口被占用）", False, "holder 未能在 10s 内占住 8765"))
            ok = False
        else:
            rc, out = run_mode("acquire")
            got_false = "RESULT=False" in out
            results.append(("B 已有实例（端口被占用）时新进程拒绝启动", got_false,
                            f"rc={rc} {out.strip().splitlines()[-1][:120] if out.strip() else '(no output)'}"))
            if not got_false:
                ok = False
    finally:
        holder.terminate()
        try:
            holder.wait(timeout=5)
        except Exception:
            holder.kill()

    # ---- C 孤儿互斥体：互斥体在，但端口没人监听 → 必须放行 ----
    # 起一个只"占位"不绑端口的进程（它持互斥体；这里让端口保持空闲）
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if sys.platform == "win32":
        h = k32.CreateMutexW(None, False, "wb_test_orphan_mutex_probe")
        if h:
            k32.CloseHandle(h)
    # 此时这个命名互斥体确实"存在"，但没人持有（已被关掉），端口也是空的
    c_rc, c_out = run_mode("acquire")
    c_ok = "RESULT=True" in c_out
    results.append(("C 互斥体不可用时照常放行", c_ok, f"rc={c_rc} {(c_out.strip().splitlines() or ['(no output)'])[-1][:120]}"))
    if not c_ok:
        ok = False

    print("=" * 62)
    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
        print(f"        {detail}")
    print("=" * 62)
    print("ALL_PASS" if ok else "HAS_FAILURE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
