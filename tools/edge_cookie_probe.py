# -*- coding: utf-8 -*-
"""从本机 Edge/Chrome 的 cookie 库读取 tgb.cn 相关 cookie 的完整值（只读，先复制副本）。
输出：tools/_edge_cookies.json（诊断用，含完整值，不提交任何远端）
"""
import base64
import ctypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from ctypes import wintypes

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_edge_cookies.json")
REPORT = []


def log(*a):
    REPORT.append(" ".join(str(x) for x in a))


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_decrypt(data: bytes):
    """调用 Windows DPAPI CryptUnprotectData"""
    blob_in = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                               ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        return None
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def get_master_key(user_data_dir: str):
    ls = os.path.join(user_data_dir, "Local State")
    if not os.path.isfile(ls):
        return None, "no Local State"
    with open(ls, "r", encoding="utf-8", errors="ignore") as f:
        j = json.load(f)
    enc = j.get("os_crypt", {}).get("encrypted_key")
    if not enc:
        return None, "no encrypted_key"
    raw = base64.b64decode(enc)
    if raw[:5] != b"DPAPI":
        return None, "unexpected prefix " + repr(raw[:5])
    key = dpapi_decrypt(raw[5:])
    if not key:
        return None, "dpapi failed"
    return key, "ok"


def decrypt_value(enc: bytes, key: bytes):
    if not enc:
        return ""
    if enc[:3] in (b"v10", b"v11"):
        if not key:
            return None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except Exception as e:
            return "<no cryptography: %s>" % e
        nonce, ct = enc[3:15], enc[15:]
        try:
            return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8", "replace")
        except Exception as e:
            return "<aesgcm fail: %s>" % e
    if enc[:3] == b"v20":
        return "<v20 App-Bound Encryption - 本地无法解密>"
    plain = dpapi_decrypt(enc)
    if plain is not None:
        return plain.decode("utf-8", "replace")
    return "<unknown encryption>"


def main():
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        ("Edge", os.path.join(local, r"Microsoft\Edge\User Data")),
        ("Chrome", os.path.join(local, r"Google\Chrome\User Data")),
    ]
    result = {"found": [], "probes": []}
    for brand, udd in candidates:
        if not os.path.isdir(udd):
            log("[%s] 未安装/无数据目录: %s" % (brand, udd))
            continue
        log("[%s] 数据目录存在: %s" % (brand, udd))
        profiles = []
        for name in os.listdir(udd):
            p = os.path.join(udd, name)
            if os.path.isdir(p) and (name == "Default" or name.startswith("Profile")):
                profiles.append(name)
        log("   profiles: %s" % ", ".join(sorted(profiles)))
        for pf in sorted(profiles):
            ck = os.path.join(udd, pf, "Network", "Cookies")
            if not os.path.isfile(ck):
                continue
            query = ("SELECT host_key,name,encrypted_value,path,expires_utc,is_secure,is_httponly "
                     "FROM cookies WHERE host_key LIKE '%tgb%' ORDER BY host_key,name")
            rows = None
            # 方案1：浏览器运行中 → immutable 只读 URI 直连（绕过独占锁）
            try:
                from urllib.parse import quote
                uri = "file:%s?immutable=1&mode=ro" % quote(ck.replace("\\", "/"), safe="/:")
                con = sqlite3.connect(uri, uri=True)
                rows = con.execute(query).fetchall()
                con.close()
                log("   读取方式: immutable 直连 (浏览器运行中)")
            except Exception as e1:
                log("   immutable 直连失败: %s -> 退回复制副本" % e1)
                tmp = os.path.join(tempfile.gettempdir(), "ck_%s_%s.db" % (brand, pf))
                try:
                    shutil.copy2(ck, tmp)
                    con = sqlite3.connect(tmp)
                    rows = con.execute(query).fetchall()
                    con.close()
                    log("   读取方式: 复制副本")
                except Exception as e2:
                    log("   读取失败: %s" % e2)
                    continue
            log("   [%s/%s] 命中 tgb 相关 cookie %d 条" % (brand, pf, len(rows)))
            key, msg = get_master_key(udd)
            log("    master key: %s" % msg)
            for host, name, enc, path, exp, sec, http in rows:
                val = decrypt_value(enc, key)
                log("      %-16s %-46s len=%s %s" % (host, name, len(val) if val else 0,
                                                     (val or "")[:70]))
                result["probes"].append({
                    "brand": brand, "profile": pf, "host": host, "name": name,
                    "value": val, "path": path, "expires_utc": exp,
                    "is_secure": sec, "is_httponly": http,
                })
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log("已写出 %s" % OUT)


if __name__ == "__main__":
    main()
    with open(os.path.join(os.path.dirname(OUT), "_edge_cookie_probe.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT))
    print("\n".join(REPORT))
