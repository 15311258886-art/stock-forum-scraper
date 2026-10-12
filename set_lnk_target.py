# -*- coding: utf-8 -*-
"""改写桌面 .lnk 的目标为「浏览器模式」启动器（绕过 WebView2 原生窗口崩溃）。

实现复用 set_lnk_icon.py 的纯 ctypes COM 方案（CoCreateInstance(CLSID_ShellLink)
→ IPersistFile::Load → SetIDList(SHParseDisplayName)/SetArguments/SetWorkingDirectory
→ IPersistFile::Save）。

注意：IShellLinkW 无 SetPath，设置目标路径需 SHParseDisplayName 得到 PIDL 后
SetIDList（vtable 第 5 项）。写入前自动备份为 *.lnk.bak-YYYYmmdd-HHMMSS。

用法（直接运行即把默认桌面 .lnk 切到浏览器模式）：
    python set_lnk_target.py
"""
from __future__ import annotations

import ctypes
import os
import shutil
import time
from ctypes import POINTER, byref, c_int, c_long, c_ulong, c_ushort, c_ubyte, c_void_p, c_wchar_p
from ctypes import wintypes

HRESULT = c_long
ole32 = ctypes.windll.ole32
shell32 = ctypes.windll.shell32
CLSCTX_INPROC_SERVER = 0x1
STGM_READWRITE = 0x00000002

shell32.SHParseDisplayName.argtypes = [c_wchar_p, c_void_p, POINTER(c_void_p), c_ulong, POINTER(c_ulong)]
shell32.SHParseDisplayName.restype = HRESULT


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", c_ubyte * 8)]


def _guid(s: str) -> GUID:
    g = GUID()
    ole32.CLSIDFromString(c_wchar_p(s), byref(g))
    return g


def _vfn(ptr, index, prototype):
    vtbl = ctypes.cast(ptr, POINTER(c_void_p))[0]
    entry = ctypes.cast(vtbl, POINTER(c_void_p))[index]
    return prototype(entry)


def set_target(lnk: str, target: str, args: str, workdir: str) -> None:
    lnk = os.path.abspath(lnk)
    if not os.path.exists(lnk):
        raise FileNotFoundError(lnk)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(lnk, f"{lnk}.bak-{stamp}")

    ole32.CoInitializeEx(None, 2)  # APARTMENTTHREADED
    CLSID_ShellLink = _guid("{00021401-0000-0000-C000-000000000046}")
    IID_IShellLinkW = _guid("{000214F9-0000-0000-C000-000000000046}")
    IID_IPersistFile = _guid("{0000010B-0000-0000-C000-000000000046}")

    psl = c_void_p()
    hr = ole32.CoCreateInstance(byref(CLSID_ShellLink), None, CLSCTX_INPROC_SERVER,
                                byref(IID_IShellLinkW), byref(psl))
    if hr != 0 or not psl:
        raise OSError(f"CoCreateInstance(ShellLink) failed hr={hr:#x}")

    try:
        QI = _vfn(psl, 0, ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p)))
        ppf = c_void_p()
        QI(psl, byref(IID_IPersistFile), byref(ppf))

        Load = _vfn(ppf, 5, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, wintypes.DWORD))
        Save = _vfn(ppf, 6, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, wintypes.BOOL))
        SetIDList = _vfn(psl, 5, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p))
        SetArgs = _vfn(psl, 11, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p))
        SetWDir = _vfn(psl, 9, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p))

        hr = Load(ppf, lnk, STGM_READWRITE)
        if hr != 0:
            raise OSError(f"IPersistFile::Load failed hr={hr:#x}")

        # 目标路径 -> PIDL -> SetIDList
        pidl = c_void_p()
        hr = shell32.SHParseDisplayName(c_wchar_p(target), None, byref(pidl), 0, None)
        if hr != 0 or not pidl:
            raise OSError(f"SHParseDisplayName failed hr={hr:#x}")
        hr = SetIDList(psl, pidl)
        if hr != 0:
            raise OSError(f"IShellLink::SetIDList failed hr={hr:#x}")

        hr = SetArgs(psl, args)
        if hr != 0:
            raise OSError(f"IShellLink::SetArguments failed hr={hr:#x}")
        hr = SetWDir(psl, workdir)
        if hr != 0:
            raise OSError(f"IShellLink::SetWorkingDirectory failed hr={hr:#x}")

        hr = Save(ppf, lnk, True)
        if hr != 0:
            raise OSError(f"IPersistFile::Save failed hr={hr:#x}")

        _vfn(ppf, 2, ctypes.WINFUNCTYPE(c_ulong, c_void_p))(ppf)
        print(f"SET_TARGET_OK lnk={lnk} backup={lnk}.bak-{stamp}")
        print(f"  target={target}")
        print(f"  args  ={args}")
        print(f"  wdir  ={workdir}")
    finally:
        _vfn(psl, 2, ctypes.WINFUNCTYPE(c_ulong, c_void_p))(psl)


if __name__ == "__main__":
    ROOT = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper"
    LNK = r"C:\Users\野比&大熊\Desktop\股票论坛可视化查看器.lnk"
    TARGET = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")
    # ⚠️ 必须加引号：本项目路径含空格（"AI Agent部署知识库"），
    #    .lnk 的 Arguments 字段是原样拼接的，不加引号会被拆成两个 token，
    #    pythonw/python 会把 "F:\AI" 当脚本路径 → 静默失败（双击毫无反应）。
    ARGS = f'"{os.path.join(ROOT, "launch_viewer.py")}"'
    WDIR = ROOT
    set_target(LNK, TARGET, ARGS, WDIR)
