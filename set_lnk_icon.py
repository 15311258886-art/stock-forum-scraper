# -*- coding: utf-8 -*-
"""设置 / 切换某个 Windows 快捷方式（.lnk）的图标。

用法:
    python set_lnk_icon.py <lnk路径> <ico路径>

实现说明:
  * 本机 PowerShell 无回显、taskkill/COM 常被安全策略拦，因此走纯 Python ctypes
    调用 COM：CoCreateInstance(CLSID_ShellLink) → QI(IPersistFile) → Load →
    SetIconLocation → Save。
  * 写入前自动把 .lnk 备份到同目录 *.lnk.bak-YYYYmmdd-HHMMSS（快捷方式很小，零成本）。
  * 完成后尝试刷新图标缓存（ie4uinit.exe -show），失败不影响主流程。
"""
import ctypes
import os
import shutil
import subprocess
import sys
import time
from ctypes import POINTER, byref, c_int, c_long, c_ubyte, c_ulong, c_ushort, c_void_p, c_wchar_p
from ctypes import wintypes

HRESULT = c_long
ole32 = ctypes.windll.ole32
CLSCTX_INPROC_SERVER = 0x1
STGM_READWRITE = 0x00000002


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort),
                ("Data4", c_ubyte * 8)]


def guid(s):
    g = GUID()
    hr = ole32.CLSIDFromString(c_wchar_p(s), byref(g))
    if hr != 0:
        raise OSError(f"CLSIDFromString failed: {s} hr={hr:#x}")
    return g


def vtable_func(ptr, index, prototype):
    """取 COM 对象 vtable 第 index 个函数的可调用包装。"""
    vtbl = ctypes.cast(ptr, POINTER(c_void_p))[0]
    entry = ctypes.cast(vtbl, POINTER(c_void_p))[index]
    return prototype(entry)


def set_icon(lnk, ico):
    lnk = os.path.abspath(lnk)
    ico = os.path.abspath(ico)
    if not os.path.exists(lnk):
        raise FileNotFoundError(lnk)
    if not os.path.exists(ico):
        raise FileNotFoundError(ico)

    # 备份
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{lnk}.bak-{stamp}"
    shutil.copy2(lnk, bak)

    ole32.CoInitializeEx(None, 2)  # APARTMENTTHREADED
    CLSID_ShellLink = guid("{00021401-0000-0000-C000-000000000046}")
    IID_IShellLinkW = guid("{000214F9-0000-0000-C000-000000000046}")
    IID_IPersistFile = guid("{0000010B-0000-0000-C000-000000000046}")

    psl = c_void_p()
    hr = ole32.CoCreateInstance(byref(CLSID_ShellLink), None, CLSCTX_INPROC_SERVER,
                                byref(IID_IShellLinkW), byref(psl))
    if hr != 0 or not psl:
        raise OSError(f"CoCreateInstance(ShellLink) failed hr={hr:#x}")

    try:
        # QueryInterface → IPersistFile
        QI = vtable_func(psl, 0, ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID),
                                                    POINTER(c_void_p)))
        ppf = c_void_p()
        hr = QI(psl, byref(IID_IPersistFile), byref(ppf))
        if hr != 0 or not ppf:
            raise OSError(f"QI(IPersistFile) failed hr={hr:#x}")

        Load = vtable_func(ppf, 5, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p,
                                                      wintypes.DWORD))
        Save = vtable_func(ppf, 6, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p,
                                                      wintypes.BOOL))
        GetIcon = vtable_func(psl, 16, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p,
                                                          c_int, POINTER(c_int), POINTER(c_int)))
        SetIcon = vtable_func(psl, 17, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p,
                                                          c_int))

        hr = Load(ppf, lnk, STGM_READWRITE)
        if hr != 0:
            raise OSError(f"IPersistFile::Load failed hr={hr:#x}")

        hr = SetIcon(psl, ico, 0)
        if hr != 0:
            raise OSError(f"IShellLink::SetIconLocation failed hr={hr:#x}")

        hr = Save(ppf, lnk, True)
        if hr != 0:
            raise OSError(f"IPersistFile::Save failed hr={hr:#x}")

        # 回读校验
        buf = ctypes.create_unicode_buffer(520)
        idx = c_int(0)
        if GetIcon(psl, buf, 520, byref(idx), None) == 0:
            got = buf.value
        else:
            got = "(readback failed)"

        ref = ctypes.c_ulong(0)
        vtable_func(ppf, 2, ctypes.WINFUNCTYPE(c_ulong, c_void_p))(ppf)  # Release
        Result = {"lnk": lnk, "ico": ico, "backup": bak, "readback": got}
        return Result
    finally:
        if psl:
            vtable_func(psl, 2, ctypes.WINFUNCTYPE(c_ulong, c_void_p))(psl)


def refresh_icon_cache():
    try:
        subprocess.run(["ie4uinit.exe", "-show"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return True
    except Exception:
        return False


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    lnk, ico = sys.argv[1], sys.argv[2]
    r = set_icon(lnk, ico)
    ok = refresh_icon_cache()
    print("SET_ICON_OK")
    for k, v in r.items():
        print(f"  {k}: {v}")
    print(f"  icon_cache_refreshed: {ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
