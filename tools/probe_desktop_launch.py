# -*- coding: utf-8 -*-
"""探针：复刻 `restart_viewer.py` 的**脱离式**拉起方式，回答「desktop_app 会不会静默消失」。

为什么要单独一个探针（2026-11-01 工单②）：
    `restart_viewer.py` 用 `DETACHED_PROCESS | NEW_PROCESS_GROUP` 拉起 desktop_app，
    然后自己退出。现象是 desktop_app 4~5 秒后消失、日志空。要判断这是
    "应用自己的问题"还是"父进程/作业对象把它带走了"，必须把**拉起方式**单独复现一次：
    同样是 detached、同样 stdin=DEVNULL、同样 stdout→文件，唯一区别是不去动 8765 上的后端。

用法：
    .venv/Scripts/python.exe tools/probe_desktop_launch.py [--seconds 20]

输出：子进程 pid / 存活秒数 / 退出码 / 日志尾部三行。
判定：
    · 存活到探针结束（探针会 terminate 它）→ 脱离式拉起**没问题**，之前的消失来自父会话/作业对象；
    · 提前退出且日志停在某一步 → 日志里的最后一行就是答案（这正是本轮加日志的目的）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "viewer", "logs", "desktop_app.log")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0, help="观察时长（秒）")
    args = ap.parse_args()

    out_path = os.path.join(ROOT, "viewer", "logs", "probe_detached_stdout.log")
    fh = open(out_path, "ab", buffering=0)
    creation = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    # --allow-multi：探针不抢单实例锁，避免把桌面上那个正常窗口挤掉
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join("viewer", "desktop_app.py"), "--allow-multi"],
        cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=creation, close_fds=True,
    )
    print("[probe] spawned pid=%s creationflags=%#x (DETACHED|NEW_GROUP)" % (proc.pid, creation))

    t0 = time.time()
    polls = 0
    while time.time() - t0 < args.seconds:
        rc = proc.poll()
        polls += 1
        if rc is not None:
            print("[probe] 子进程在 +%.1fs 退出，rc=%s（未活到探针结束）" % (time.time() - t0, rc))
            break
        time.sleep(0.5)
    else:
        print("[probe] 子进程活满 %.0fs（仍在运行），由探针终止" % args.seconds)
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:                                # noqa: BLE001
            proc.kill()
        print("[probe] 已终止 rc=%s" % proc.returncode)

    print("[probe] 轮询次数=%d  脱离式 stdout 落盘=%s" % (polls, out_path))
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip() for ln in f if ln.strip()]
        print("[probe] 日志尾部（%d 行）:" % len(lines))
        for ln in lines[-6:]:
            print("    " + ln)
    else:
        print("[probe] ⚠ %s 不存在 —— 进程连日志都没建起来，那就是死在 import/解释器阶段" % LOG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
