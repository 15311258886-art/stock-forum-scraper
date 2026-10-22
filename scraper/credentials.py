"""本地加密凭据存储（Fernet 对称加密）。

支持两种登录凭据（可同时存在，互不覆盖）：

1. **账号密码**（username / password）
   适用于仍有"用户名+密码"表单登录的站点。

2. **Cookie**（cookies 列表）
   适用于【扫码登录 / 短信验证码 / MFA】等**无法程序化提交表单**的站点
   （淘股吧即属此类：现行登录只有微信扫码与手机验证码，无密码入口）。
   做法：你在本机浏览器手动登录一次 → 导出 Cookie → 我加密存到本机 →
   程序带 Cookie 抓取。Cookie 有过期时间，失效后重新登录一次即可。

设计原则：
- 不实现自动注册 / 批量建号 / 验证码破解。账号由你本人手动注册与登录。
- 密钥文件权限 600，仅当前用户可读；凭据以密文落盘，不存明文、不上传。
- 凭据默认存于 ~/.workbuddy/stock-scraper-creds/，不在项目目录内，避免误提交。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet

STORE_DIR = Path(os.path.expanduser("~/.workbuddy/stock-scraper-creds"))
KEY_FILE = STORE_DIR / ".key"
CRED_FILE = STORE_DIR / "credentials.enc"


def _get_fernet() -> Fernet:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_FILE.exists():
        KEY_FILE.write_bytes(Fernet.generate_key())
        try:
            KEY_FILE.chmod(0o600)
        except OSError:
            pass
    return Fernet(KEY_FILE.read_bytes())


def _load_all(f: Fernet) -> dict:
    if not CRED_FILE.exists():
        return {}
    try:
        return json.loads(f.decrypt(CRED_FILE.read_bytes()).decode("utf-8"))
    except Exception:
        return {}


def _save_all(f: Fernet, data: dict) -> None:
    CRED_FILE.write_bytes(f.encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8")))
    try:
        CRED_FILE.chmod(0o600)
    except OSError:
        pass


def _entry(f: Fernet, site: str) -> dict:
    """取（或初始化）某站点的凭据条目，保证不覆盖另一类凭据。"""
    data = _load_all(f)
    return data, data.setdefault(site, {})


# --------------------------------------------------------------------------- #
# 账号密码
# --------------------------------------------------------------------------- #
def save_credential(site: str, username: str, password: str) -> None:
    f = _get_fernet()
    data, entry = _entry(f, site)
    entry["username"] = username
    entry["password"] = password
    entry["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_all(f, data)


def get_credential(site: str) -> dict | None:
    """仅当账号密码齐备时返回，避免只存了 Cookie 时被误当成"有账号密码"。"""
    entry = _load_all(_get_fernet()).get(site) or {}
    if entry.get("username") and entry.get("password"):
        return {"username": entry["username"], "password": entry["password"]}
    return None


# --------------------------------------------------------------------------- #
# Cookie
# --------------------------------------------------------------------------- #
def save_cookies(site: str, cookies: list | dict, meta: dict | None = None) -> int:
    """保存 Cookie。

    cookies 可为：
      - list[dict]：每项含 name / value，可选 domain / path / expires
      - dict：{name: value} 简写
    返回保存的条数。
    """
    if isinstance(cookies, dict):
        cookies = [{"name": k, "value": v} for k, v in cookies.items()]
    norm: list[dict] = []
    for c in cookies or []:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        item = {"name": str(name), "value": str(value)}
        for k in ("domain", "path", "expires", "secure", "httpOnly"):
            if c.get(k) not in (None, ""):
                item[k] = c[k]
        norm.append(item)
    if not norm:
        return 0
    f = _get_fernet()
    data, entry = _entry(f, site)
    entry["cookies"] = norm
    entry["cookie_count"] = len(norm)
    entry["cookie_saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if meta:
        entry["cookie_meta"] = meta
    _save_all(f, data)
    return len(norm)


def get_cookies(site: str) -> list | None:
    entry = _load_all(_get_fernet()).get(site) or {}
    ck = entry.get("cookies")
    return ck if isinstance(ck, list) and ck else None


def get_cookie_meta(site: str) -> dict:
    entry = _load_all(_get_fernet()).get(site) or {}
    return {
        "count": entry.get("cookie_count") or len(entry.get("cookies") or []),
        "saved_at": entry.get("cookie_saved_at", ""),
        **(entry.get("cookie_meta") or {}),
    }


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
def describe(site: str) -> dict:
    """给 `credentials list` 用的脱敏描述（不回显密码与 Cookie 值）。"""
    entry = _load_all(_get_fernet()).get(site) or {}
    return {
        "site": site,
        "has_password": bool(entry.get("username") and entry.get("password")),
        "username": entry.get("username", ""),
        "saved_at": entry.get("saved_at", ""),
        "cookie_count": entry.get("cookie_count") or len(entry.get("cookies") or []),
        "cookie_saved_at": entry.get("cookie_saved_at", ""),
    }


def list_sites() -> list[str]:
    return sorted(_load_all(_get_fernet()).keys())


def delete_credential(site: str) -> bool:
    f = _get_fernet()
    data = _load_all(f)
    if site in data:
        del data[site]
        _save_all(f, data)
        return True
    return False


# --------------------------------------------------------------------------- #
# 通用密钥（如 DeepSeek API Key）
# --------------------------------------------------------------------------- #
def save_secret(name: str, value: str) -> None:
    """把任意密钥以密文存进同一份凭据库（复用本文件的 Fernet 密钥）。"""
    f = _get_fernet()
    data, entry = _entry(f, "secrets")
    entry[name] = value
    _save_all(f, data)


def load_secret(name: str) -> str:
    """取回 save_secret 存的密钥；不存在返回空串。"""
    entry = _load_all(_get_fernet()).get("secrets") or {}
    v = entry.get(name)
    return v if isinstance(v, str) else ""

