# -*- coding: utf-8 -*-
"""按端口找监听进程 PID（Windows iphlpapi），用来安全地停掉占用端口的旧实例。

用法：
    python _find_listener.py            # 打印所有 LISTEN 端口
    python _find_listener.py 8765       # 只查 8765
"""
from __future__ import annotations

import ctypes
import socket
import sys


class MIB_TCPEX6ROW(ctypes.Structure):
    """IPv4 / IPv6 通用；这里只用 LISTEN 表（AF_INET6 + TCP_TABLE_LISTENER）。"""

    _fields_ = [
        ("dwState", ctypes.c_ulong),
        ("dwLocalAddr", ctypes.c_ulong),   # IPv4 地址
        ("dwLocalPort", ctypes.c_ulong),   # 网络序端口
        ("dwRemoteAddr", ctypes.c_ulong),
        ("dwRemotePort", ctypes.c_ulong),
        ("dwOwningPid", ctypes.c_ulong),
    ]


def listeners() -> list[tuple[str, int, int]]:
    iphlp = ctypes.windll.iphlpapi
    buf = ctypes.create_string_buffer(1 << 20)
    size = ctypes.c_ulong(ctypes.sizeof(buf))
    rc = iphlp.GetExtendedTcpTable(ctypes.byref(buf), ctypes.byref(size),
                                   True, socket.AF_INET6, 5, 0)  # TCP_TABLE_LISTENER
    if rc != 0:
        rc = iphlp.GetExtendedTcpTable(ctypes.byref(buf), ctypes.byref(size),
                                       True, socket.AF_INET, 2, 0)
    out: list[tuple[str, int, int]] = []
    if rc == 0:
        arr = ctypes.cast(buf, ctypes.POINTER(MIB_TCPEX6ROW))
        n = size.value // ctypes.sizeof(MIB_TCPEX6ROW)
        for i in range(n):
            r = arr[i]
            if r.dwState == 2:  # LISTEN
                ip = socket.ntohl(r.dwLocalAddr)
                port = socket.ntohs(r.dwLocalPort & 0xFFFF)
                out.append((f"{ip >> 24 & 255}.{ip >> 16 & 255}.{ip >> 8 & 255}.{ip & 255}",
                            port, r.dwOwningPid))
    return sorted(set(out))


def main() -> int:
    want = int(sys.argv[1]) if len(sys.argv) > 1 else None
    rows = listeners()
    if want is None:
        for ip, port, pid in rows:
            print(f"{ip}:{port}  pid={pid}")
        return 0
    hit = [(ip, port, pid) for ip, port, pid in rows if port == want]
    if not hit:
        print(f"端口 {want} 没有进程在监听")
        return 1
    for ip, port, pid in hit:
        print(f"端口 {want} 正在被 pid={pid} 监听（{ip}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
