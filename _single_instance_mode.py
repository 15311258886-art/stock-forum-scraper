"""单实例互斥自测用的两种子进程模式（勿在业务代码里引用）。

    hold      持住命名互斥体 + 绑住端口 8765，存活 HOLD_SECONDS 秒（模拟已在运行的实例）
    acquire   直接调 _acquire_single_instance() 并打印 RESULT=True/False（模拟双击第二个）
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

HOLD_SECONDS = 18


def _sink() -> None:
    """占住端口的哑服务：只 accept 后立刻关闭，不返回任何业务数据。"""
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 8765))
        srv.listen(8)
        srv.settimeout(1.0)
        end = time.time() + HOLD_SECONDS + 3
        while time.time() < end:
            try:
                c, _ = srv.accept()
                c.close()
            except socket.timeout:
                continue
            except OSError:
                break
    except OSError:
        pass
    finally:
        try:
            srv.close()
        except Exception:
            pass


def mode_hold() -> None:
    from viewer import desktop_app as DA
    got = DA._acquire_single_instance()
    print(f"HOLD acquired={got}", flush=True)
    threading.Thread(target=_sink, daemon=True).start()
    time.sleep(HOLD_SECONDS)


def mode_acquire() -> None:
    from viewer import desktop_app as DA
    got = DA._acquire_single_instance()
    print(f"RESULT={got}", flush=True)


if __name__ == "__main__":
    m = sys.argv[1] if len(sys.argv) > 1 else ""
    if m == "hold":
        mode_hold()
    else:
        mode_acquire()
