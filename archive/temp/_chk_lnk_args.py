# -*- coding: utf-8 -*-
"""只读探针：读取桌面 .lnk 的 target/args/workdir/icon（纯 ctypes，不写入）。"""
from __future__ import annotations
import sys
import ctypes
from ctypes import POINTER, byref, c_int, c_ulong, c_ushort, c_ubyte, c_void_p, c_wchar_p
from ctypes import wintypes

HRESULT = ctypes.c_long
ole32 = ctypes.windll.ole32

# IShellLinkW vtable (after IUnknown 0/1/2):
# 3 GetPath, 4 GetIDList, 5 SetIDList, 6 GetDescription, 7 SetDescription,
# 8 GetWorkingDirectory, 9 SetWorkingDirectory, 10 GetArguments, 11 SetArguments,
# 12 GetHotkey, 13 SetHotkey, 14 GetShowCmd, 15 SetShowCmd,
# 16 GetIconLocation, 17 SetIconLocation, 18 Resolve, 19 SetPath


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", c_ushort), ("Data3", c_ushort), ("Data4", c_ubyte * 8)]


def _guid(s: str) -> GUID:
    g = GUID()
    ole32.CLSIDFromString(c_wchar_p(s), byref(g))
    return g


def _vfn(ptr, index, proto):
    vtbl = ctypes.cast(ptr, POINTER(c_void_p))[0]
    entry = ctypes.cast(vtbl, POINTER(c_void_p))[index]
    return proto(entry)


LNK = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\野比&大熊\Desktop\股票论坛可视化查看器.lnk"

ole32.CoInitializeEx(None, 2)
CLSID_ShellLink = _guid("{00021401-0000-0000-C000-000000000046}")
IID_IShellLinkW = _guid("{000214F9-0000-0000-C000-000000000046}")
IID_IPersistFile = _guid("{0000010B-0000-0000-C000-000000000046}")

psl = c_void_p()
hr = ole32.CoCreateInstance(byref(CLSID_ShellLink), None, 0x1, byref(IID_IShellLinkW), byref(psl))
print("CoCreateInstance hr =", hex(hr), "psl =", psl)

QI = _vfn(psl, 0, ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p)))
ppf = c_void_p()
QI(psl, byref(IID_IPersistFile), byref(ppf))
Load = _vfn(ppf, 5, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, wintypes.DWORD))
hr = Load(ppf, LNK, 0)  # STGM_READ
print("Load hr =", hex(hr))

GetPath = _vfn(psl, 3, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int, c_void_p, c_ulong))
GetWDir = _vfn(psl, 8, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int))
GetArgs = _vfn(psl, 10, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int))
GetIcon = _vfn(psl, 16, ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int, POINTER(c_int)))

b1 = ctypes.create_unicode_buffer(4096)
fd = ctypes.create_string_buffer(1024)
print("GetPath   hr =", hex(GetPath(psl, b1, 4096, byref(fd), 0)), "=>", repr(b1.value))

b2 = ctypes.create_unicode_buffer(4096)
print("GetWDir   hr =", hex(GetWDir(psl, b2, 4096)), "=>", repr(b2.value))

b3 = ctypes.create_unicode_buffer(4096)
print("GetArgs   hr =", hex(GetArgs(psl, b3, 4096)), "=>", repr(b3.value))

b4 = ctypes.create_unicode_buffer(4096)
idx = c_int()
print("GetIcon   hr =", hex(GetIcon(psl, b4, 4096, byref(idx))), "=>", repr(b4.value), "idx", idx.value)

# 原始字节：是否含 UTF-16LE 的 "F: （0x22 0x00 46 0x00 3A 0x00）
d = open(LNK, "rb").read()
q = bytes([0x22, 0x00, 0x46, 0x00, 0x3A, 0x00])
f = bytes([0x46, 0x00, 0x3A, 0x00])
print('raw 含 "F: :', q in d)
print("raw 含  F: :", f in d)

_vfn(psl, 2, ctypes.WINFUNCTYPE(c_ulong, c_void_p))(psl)
_vfn(ppf, 2, ctypes.WINFUNCTYPE(c_ulong, c_void_p))(ppf)
ole32.CoUninitialize()
