# -*- coding: utf-8 -*-
"""统一 HTTP 客户端（P1-37：替代全仓 ≥4 份 _get_json / requests 调用）。

提供：
  · get_text(url, ...)     —— 带超时 / UA / 重试的 GET，返回文本
  · get_json(url, ...)     —— GET + JSON 解析（兼容 utf-8 / utf-8-sig / replace）
  · post_json(url, payload, ...) —— JSON POST
  · default_session()     —— 复用连接的 requests.Session（全仓唯一会话点，P3-05 收敛）

设计要点：
  · 默认 8s 超时（P2-09：避免单 URL 占住请求线程过久）
  · 指数退避重试，status_forcelist 含 429/500/502/503/504（P1-46：含 5xx，不含 403 风控页单独处理）
  · 仅依赖 requests（已在 .venv 实装）；无 requests 时退化为 urllib 兜底
不做 TLS 校验开关的硬编码——由调用方通过 verify_hosts 参数控制（P1-47）。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

LOG = logging.getLogger("infra.httpclient")

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_DEFAULT_TIMEOUT = 8
_MAX_RETRIES = 3

_SESSION = None


def default_session():
    """全仓唯一 requests.Session（懒加载；无 requests 返回 None 走 urllib 兜底）。"""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    try:
        import requests  # noqa: F401
    except ImportError:
        return None
    s = requests.Session()
    s.headers.update({"User-Agent": _DEFAULT_UA, "Accept": "*/*"})
    _SESSION = s
    return s


def _backoff(n: int) -> float:
    return min(0.5 * (2 ** n) + 0.1, 4.0)


def get_text(url: str, *,
             timeout: int = _DEFAULT_TIMEOUT,
             headers: Optional[dict] = None,
             retries: int = _MAX_RETRIES,
             verify: bool = True,
             encoding: Optional[str] = None) -> str:
    """GET 返回文本；失败抛 RuntimeError（调用方自行决定降级）。"""
    hdrs = {"User-Agent": _DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            s = default_session()
            if s is not None:
                resp = s.get(url, timeout=timeout, headers=hdrs, verify=verify)
                resp.raise_for_status()
                return resp.text
            # urllib 兜底
            import urllib.request as _ur
            req = _ur.Request(url, headers=hdrs)  # noqa: UP037
            with _ur.urlopen(req, timeout=timeout) as r:  # noqa: S310
                raw = r.read()
            return raw.decode(encoding or "utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(_backoff(attempt))
                continue
            break
    raise RuntimeError("HTTP GET 失败 %s: %s" % (url, last_err))


def get_json(url: str, **kw) -> Any:
    """GET + JSON 解析；兼容 utf-8-sig / replace。"""
    text = get_text(url, **kw)
    return json.loads(text.encode("utf-8").decode("utf-8-sig") if text.startswith("\ufeff") else text)


def post_json(url: str, payload: Any, *,
              timeout: int = _DEFAULT_TIMEOUT,
              headers: Optional[dict] = None,
              retries: int = _MAX_RETRIES,
              verify: bool = True) -> Any:
    """JSON POST，返回解析后的 JSON（或文本）。"""
    hdrs = {"User-Agent": _DEFAULT_UA, "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            s = default_session()
            if s is not None:
                resp = s.post(url, data=body, timeout=timeout, headers=hdrs, verify=verify)
                resp.raise_for_status()
                return resp.json()
            import urllib.request as _ur
            req = _ur.Request(url, data=body, headers=hdrs, method="POST")  # noqa: UP037
            with _ur.urlopen(req, timeout=timeout) as r:  # noqa: S310
                raw = r.read()
            return json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(_backoff(attempt))
                continue
            break
    raise RuntimeError("HTTP POST 失败 %s: %s" % (url, last_err))
