"""以「脱离进程 + 全量日志重定向」方式拉起查看器后端（排障用）。

为什么需要它：直接用 pythonw 启动时 stdout/stderr 全部丢弃，服务一旦在启动阶段
异常退出，日志里什么都看不到，只能看到「打开页面一堆加载失败」。
本脚本把子进程的输出全部写到 viewer/logs/_boot.log，且用 DETACHED_PROCESS 脱离父会话。

用法：pythonw tools/run_server_detached.py
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "viewer", "logs", "_boot.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)

fh = open(LOG, "ab", buffering=0)
fh.write(("\n===== 拉起 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
subprocess.Popen(
    [sys.executable, "-u", "-m", "viewer.server"],
    cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    creationflags=0x00000008,          # DETACHED_PROCESS：脱离当前会话，不会随终端退出
)
