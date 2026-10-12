# -*- coding: utf-8 -*-
"""只读探针：① 检测单实例互斥体是否被占用；② 列出 python.exe 的完整路径。"""
from __future__ import annotations
import ctypes
from ctypes import wintypes

k32 = ctypes.windll.kernel32

# ---- ① 互斥体 ----
NAME = "Local\\stock_forum_viewer_8765"
k32.SetLastError(0)
h = k32.CreateMutexW(None, False, NAME)
err = k32.GetLastError()
print("MUTEX_NAME   =", NAME)
print("MUTEX_STATUS =", "ALREADY_EXISTS(有实例持锁)" if err == 183 else "FREE(无人持锁)", "err=", err)
if h:
    k32.CloseHandle(h)

# ---- ② 枚举进程，打印 python 的 exe 路径 ----
TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
pe = PROCESSENTRY32W()
pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
print("\nPID      exe                          path")
if k32.Process32FirstW(snap, ctypes.byref(pe)):
    while True:
        if "python" in pe.szExeFile.lower():
            hp = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pe.th32ProcessID)
            path = ""
            if hp:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(size)):
                    path = buf.value
                k32.CloseHandle(hp)
            print(f"{pe.th32ProcessID:<8} {pe.szExeFile:<28} {path}")
        if not k32.Process32NextW(snap, ctypes.byref(pe)):
            break
k32.CloseHandle(snap)
