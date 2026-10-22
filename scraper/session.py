"""带速率限制与 robots.txt 检查的 HTTP 会话。

- 速率限制：按 configured 每分钟请求数平滑限速，并加入微小抖动，避免突发。
- 失败重试：对 429 / 5xx 指数退避重试。
- robots.txt：默认尊重；登录后才能访问的页面通常不受 robots 约束，
  但公开页仍建议遵守（可用 --no-robots 覆盖，但请自行确认合规）。
"""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class RateLimitedSession:
    def __init__(
        self,
        user_agent: str = DEFAULT_UA,
        rate_limit_per_min: int = 20,
        timeout: int = 15,
        retry_max: int = 3,
        respect_robots: bool = True,
        extra_headers: Optional[dict] = None,
    ):
        self.ua = user_agent
        self.min_interval = 60.0 / max(rate_limit_per_min, 1)
        self.timeout = timeout
        self.respect_robots = respect_robots
        self._last = 0.0
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        # 站点自定义 header（如 Referer/Cookie 伪装），用于绕过东财的「身份核实」风控
        if extra_headers:
            self.session.headers.update(extra_headers)
        retry = Retry(
            total=retry_max,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        rp = self._robots_cache.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                rp.set_url(base + "/robots.txt")
                import socket as _sock
                _prev_to = _sock.getdefaulttimeout()
                _sock.setdefaulttimeout(5)  # P2-10：robots.txt 抓取硬超时 5s，避免被卡死
                try:
                    rp.read()
                finally:
                    _sock.setdefaulttimeout(_prev_to)
            except Exception as e:  # noqa: BLE001
                logger.warning("robots.txt 读取失败(%s)，默认放行: %s", base, e)
                rp = None
            self._robots_cache[base] = rp
        if rp is None:
            return True
        return rp.can_fetch(self.ua, url)

    def get(self, url: str, **kwargs):
        if not self._robots_allows(url):
            raise PermissionError(
                f"robots.txt 禁止抓取: {url}（如需覆盖请使用 --no-robots，但请确认合规）"
            )
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        resp = self.session.get(url, timeout=self.timeout, **kwargs)
        self._last = time.monotonic()
        return resp

    def request(self, method: str, url: str, **kwargs):
        """供登录 POST 使用（不强制 robots 检查，但保留限速）。"""
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        self._last = time.monotonic()
        return resp
