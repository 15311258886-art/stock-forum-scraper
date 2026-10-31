# -*- coding: utf-8 -*-
"""viewer/secrets_dpapi.py — 会话凭据的静态加密（Windows DPAPI，**不依赖 pywin32**）。

## 为什么需要它

项目根长期躺着三份**明文会话凭据**：

| 文件 | 谁在读 | 内容性质 |
|---|---|---|
| `cookies.txt` | `scraper/browser_fetch.py`（雪球渲染抓取） | `xq_a_token` 等登录态 |
| `jisilu_cookie.txt` | `etf_jisilu.py`（ETF 溢价/份额） | 集思录登录态 |
| `eastmoney_cookie.txt` | `etf_track_error.py`（ETF 跟踪误差） | 东财登录态 |

它们已被 `.gitignore` 排除（不会进版本库），但**磁盘上仍是明文**：一次误压缩备份、
一次 `git add -f`、一次远程协助截屏，凭据就外流了。会话凭据的可怕之处不是"能不能解密"，
而是"它本来就不需要解密 —— 打开就是"。

## 加密方式为什么选 DPAPI

- **不引入新依赖**：项目对装包有限制（见 `requirements.txt`），所以不用 `cryptography`、
  不用 `pywin32`；`CryptProtectData`/`CryptUnprotectData` 通过 `ctypes` 直调 `crypt32.dll`。
- **密钥不落盘**：DPAPI 的根密钥由 Windows 用**当前用户的登录凭据**派生并保存在系统里。
  本模块**不保存任何密钥**，因此"把 .dpapi 文件拷走"在这台机器/这个用户之外毫无用处。
- **失败要能退回**：`load()` 在 `.dpapi` 缺失/解不开时回退明文并打 WARNING，
  绝不让一个加密改造把生产抓取链改停（本项目的老教训：为了"更安全"改坏可用性，
  最后被整段注释掉，比原来更差）。

## 边界（诚实标注，不吹）

1. **同用户的其它进程能解开**。DPAPI 防的是"文件被拷走 / 被误提交 / 被别家程序读到"，
   不防"有人已经以你这个 Windows 账号登录着"。要防后者得用硬件密钥/口令派生，
   那是另一个量级的改造。
2. **换机器 / 换用户解不开**，这是特性不是缺陷；跨机迁移请重新导入明文再 `--protect`。
3. **`load()` 回退明文时会写 WARNING**，这条日志就是迁移提醒；看见它就说明还没迁完。
4. **本模块不做文件权限（ACL）收窄**：Windows 上没有稳定的纯 ctypes ACL 写法，
   硬写容易把文件锁死给"只有 SYSTEM 能读"，反而让抓取链失败。属已知未做项。

## 用法

```python
from viewer import secrets_dpapi as secrets

text = secrets.load("jisilu_cookie.txt")      # 优先 .dpapi，回退明文并 WARNING
print(text)                                    # Netscape 格式文本 / None（不存在）
```

一次性迁移（**另跑，不在导入时做**）：

```
python tools/protect_secrets.py --check      # 只看状态，不写
python tools/protect_secrets.py --protect    # 生成 .dpapi（明文保留）
python tools/protect_secrets.py --verify     # 解回来和明文逐字节比对
python tools/protect_secrets.py --shred --yes  # 确认无误后再删明文（默认不删）
```
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as _wt
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- WARNING 限频
# 为什么需要限频：`load()` 的三个调用点（雪球渲染抓取 / 集思录 ETF / 东财跟踪误差）
#   都是**每抓一轮就调一次**。回退明文的 WARNING 若每次都打，日志里很快就会变成
#   一天几十行同样的字 —— 运维读两眼就开始无视它，那这条提醒就等于不存在，
#   迁移永远做不完。限成「同一进程、同一原因、同一天只打一条」，它才会被当成待办。
# 注意这是**日志限频，不是行为限频**：读文件的行为一次都不省。
_WARN_SEEN: dict = {}
_WARN_LOCK = threading.Lock()


def _warn_once(name: str, reason: str, msg: str, *args) -> None:
    """同一进程内，`(name, reason)` 组合每天最多打一条 WARNING。"""
    day = time.strftime("%Y-%m-%d")
    key = (name, reason)
    with _WARN_LOCK:
        if _WARN_SEEN.get(key) == day:
            return
        _WARN_SEEN[key] = day
    log.warning(msg, *args)


def reset_warn_throttle() -> None:
    """清空限频状态。仅测试与 `--check` 之类的显式盘点用，正常流程不要调。"""
    with _WARN_LOCK:
        _WARN_SEEN.clear()

# 项目根（本文件在 viewer/ 下）
ROOT = Path(__file__).resolve().parent.parent

# 受管凭据清单：逻辑名 = 文件名（都在项目根）。
# 刻意用"文件名"而不是"逻辑别名"：现有三个读取点都是 `os.path.join(BASE, 'xxx_cookie.txt')`，
# 用同名文件能让将来的改造只换一行（`open(path)` → `secrets.load(name)`），不必改约定。
MANAGED = ("cookies.txt", "jisilu_cookie.txt", "eastmoney_cookie.txt")

# DPAPI 的附加熵（secondary entropy）。它不是密钥（密钥在系统里），只是让"别的程序
# 用同一个用户身份调 CryptUnprotectData 也解不开" —— 多一道，不影响可达性。
# ⚠️ 改动这个常量会让**已存在的 .dpapi 全部解不开**（熵参与解密），要改必须同时重跑 --protect。
_ENTROPY = b"stock-forum-scraper/secrets/v1"

_SUFFIX = ".dpapi"


class SecretsUnavailable(RuntimeError):
    """DPAPI 不可用（非 Windows，或 crypt32 调用失败）。调用方应回退明文而不是崩。"""


# ------------------------------------------------------------------ DPAPI 绑定
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", _wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _load_dlls():
    """延迟绑定 crypt32/kernel32。非 Windows 直接返回 (None, None)。"""
    if os.name != "nt":
        return None, None
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _blob_p = ctypes.POINTER(_DATA_BLOB)
    crypt32.CryptProtectData.argtypes = [
        _blob_p,                    # pDataIn
        _wt.LPCWSTR,                # szDataDescr（写进 blob 的可读描述，解密时能取回）
        _blob_p,                    # pOptionalEntropy
        ctypes.c_void_p,            # pvReserved
        ctypes.c_void_p,            # pPromptStruct
        _wt.DWORD,                  # dwFlags（0 = 当前用户作用域）
        _blob_p,                    # pDataOut
    ]
    crypt32.CryptProtectData.restype = _wt.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        _blob_p,                    # pDataIn
        ctypes.POINTER(_wt.LPWSTR),  # ppszDataDescr（系统分配，需 LocalFree）
        _blob_p,                    # pOptionalEntropy
        ctypes.c_void_p,
        ctypes.c_void_p,
        _wt.DWORD,
        _blob_p,
    ]
    crypt32.CryptUnprotectData.restype = _wt.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _blob(data: bytes):
    """把 bytes 包成 DATA_BLOB，并**返回 buffer 引用**（否则 create_string_buffer 被回收 → 悬垂指针）。"""
    if not data:
        return _DATA_BLOB(0, None), None
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def available() -> bool:
    """DPAPI 在本机是否可用（只做绑定检查，不产生副作用）。"""
    try:
        crypt32, _ = _load_dlls()
        return crypt32 is not None
    except OSError:
        return False


def protect_bytes(plain: bytes, descr: str = "stock-forum-scraper secrets") -> bytes:
    """用当前 Windows 用户的 DPAPI 密钥加密。失败抛 SecretsUnavailable。"""
    crypt32, kernel32 = _load_dlls()
    if crypt32 is None:
        raise SecretsUnavailable("DPAPI 只在 Windows 可用（当前 os.name=%r）" % os.name)
    data_in, _keep_in = _blob(plain)
    entropy, _keep_ent = _blob(_ENTROPY)
    data_out = _DATA_BLOB()
    ok = crypt32.CryptProtectData(ctypes.byref(data_in), descr, ctypes.byref(entropy),
                                  None, None, 0, ctypes.byref(data_out))
    if not ok:
        raise SecretsUnavailable("CryptProtectData 失败（GetLastError=%d）"
                                 % ctypes.get_last_error())
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        if data_out.pbData:
            kernel32.LocalFree(data_out.pbData)


def unprotect_bytes(cipher: bytes) -> bytes:
    """解密。失败（换用户/换机器/熵不匹配/文件被截断）抛 SecretsUnavailable。"""
    crypt32, kernel32 = _load_dlls()
    if crypt32 is None:
        raise SecretsUnavailable("DPAPI 只在 Windows 可用（当前 os.name=%r）" % os.name)
    data_in, _keep_in = _blob(cipher)
    entropy, _keep_ent = _blob(_ENTROPY)
    data_out = _DATA_BLOB()
    descr = _wt.LPWSTR()
    ok = crypt32.CryptUnprotectData(ctypes.byref(data_in), ctypes.byref(descr),
                                    ctypes.byref(entropy), None, None, 0,
                                    ctypes.byref(data_out))
    if not ok:
        raise SecretsUnavailable("CryptUnprotectData 失败（GetLastError=%d）；"
                                 "常见原因：换了用户/机器，或熵常量改过"
                                 % ctypes.get_last_error())
    try:
        return ctypes.string_at(data_out.pbData, data_out.cbData)
    finally:
        if descr:
            kernel32.LocalFree(descr)
        if data_out.pbData:
            kernel32.LocalFree(data_out.pbData)


# ------------------------------------------------------------------ 路径与读写
def plain_path(name: str) -> Path:
    """明文路径。当前三个读取点都用项目根同名文件，保持这一点即可零改造兼容。"""
    return ROOT / name


def cipher_path(name: str) -> Path:
    return ROOT / (name + _SUFFIX)


def is_protected(name: str) -> bool:
    return cipher_path(name).is_file()


def load(name: str) -> str | None:
    """读取一个受管凭据的文本内容；不存在返回 None。

    优先 `.dpapi`（解密后按 UTF-8 解码，`errors="replace"`：Cookie 里偶有非法字节，
    不能让一个字节把整条抓取链搞停）。`.dpapi` 不存在或解不开 → 回退明文并 WARNING。

    ⚠️ 回退是**刻意**的：加密改造不能把可用性改坏（见模块 docstring 的边界第 3 条）。
    """
    cp = cipher_path(name)
    if cp.is_file():
        try:
            raw = unprotect_bytes(cp.read_bytes())
            return raw.decode("utf-8", errors="replace")
        except SecretsUnavailable as e:
            _warn_once(name, "decrypt",
                       "[secrets] %s 解密失败（%s），回退明文；请重新 --protect", name, e)
        except OSError as e:
            _warn_once(name, "read",
                       "[secrets] %s 读取失败（%s），回退明文", name, e)
    pp = plain_path(name)
    if pp.is_file():
        # 这条就是迁移提醒：看见它就说明这份凭据还是明文、还是"打开就是"。
        # 限频到一天一条（见 `_warn_once` 的注释），否则每抓一轮刷一行等于没提醒。
        _warn_once(name, "plaintext",
                   "[secrets] %s 仍是**明文**（Session 凭据可直接读取）；"
                   "请运行 `python tools/protect_secrets.py --protect` 迁移", name)
        try:
            return pp.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log.warning("[secrets] %s 明文读取失败（%s）", name, e)
            return None
    return None


def protect(name: str) -> Path:
    """把明文物加密成 `<name>.dpapi`（原子写）。明文**不删** —— 删除是 `shred()` 的事。"""
    pp = plain_path(name)
    if not pp.is_file():
        raise FileNotFoundError("没有明文可加密：%s" % pp)
    cipher = protect_bytes(pp.read_bytes(), descr="stock-forum-scraper:%s" % name)
    cp = cipher_path(name)
    tmp = cp.with_suffix(cp.suffix + ".tmp")
    tmp.write_bytes(cipher)
    os.replace(tmp, cp)          # 原子替换：避免"写了一半被读到"产生解不开的 blob
    return cp


def verify(name: str) -> bool:
    """解密结果与明文逐字节相同则 True（无明文时，只判断能否解开）。"""
    cp = cipher_path(name)
    if not cp.is_file():
        return False
    try:
        raw = unprotect_bytes(cp.read_bytes())
    except SecretsUnavailable:
        return False
    pp = plain_path(name)
    if not pp.is_file():
        return True
    return pp.read_bytes() == raw


def shred(name: str) -> bool:
    """删除明文（仅在 `--shred --yes` 下由 tools/protect_secrets.py 调用）。

    先校验 `.dpapi` 能解回同样内容，校验不过就**拒绝删除** —— 这是最后一道保险：
    宁可留一份明文，也不能把唯一一份凭据搞丢。
    """
    if not verify(name):
        log.error("[secrets] %s 的 .dpapi 校验不通过，拒绝删除明文", name)
        return False
    pp = plain_path(name)
    if pp.is_file():
        pp.unlink()
        return True
    return False


def status() -> list[dict]:
    """只读盘点（供 tools/protect_secrets.py --check 与巡检用）。**不返回内容。**"""
    out = []
    for name in MANAGED:
        pp, cp = plain_path(name), cipher_path(name)
        out.append({
            "name": name,
            "plain_exists": pp.is_file(),
            "plain_bytes": pp.stat().st_size if pp.is_file() else 0,
            "plain_mtime": (pp.stat().st_mtime if pp.is_file() else None),
            "cipher_exists": cp.is_file(),
            "cipher_bytes": cp.stat().st_size if cp.is_file() else 0,
            "cipher_ok": verify(name) if cp.is_file() else False,
        })
    return out
