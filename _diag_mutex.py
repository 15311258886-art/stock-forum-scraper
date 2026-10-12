"""诊断：当前 8765 是否被占、命名互斥体是否真在、CreateMutex 返回什么错误码。"""
from __future__ import annotations

import ctypes
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.settimeout(0.5)
    rc = s.connect_ex(("127.0.0.1", 8765))
print("port8765_in_use =", rc == 0)

from viewer import desktop_app as DA
print("MUTEX_NAME =", DA.MUTEX_NAME)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.SetLastError(0)
h = k32.CreateMutexW(None, False, DA.MUTEX_NAME)
err = ctypes.get_last_error()
print(f"CreateMutexW handle={h} last_error={err}")
if h and err != 183:
    print("-> 本次是真新建，可以关句柄")
    k32.CloseHandle(h)
else:
    print("-> 命中已有实例")
