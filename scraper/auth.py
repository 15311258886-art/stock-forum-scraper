"""登录模块：使用用户【手动注册/手动登录】的凭据完成登录并维持会话。

两种登录方式（按站点实际情况自动选择）：

- **Cookie 注入**（`auth_mode: cookie`）：适用于登录入口为「扫码 / 短信验证码 / MFA」的站点
  （淘股吧即属此类）。你在浏览器手动登录一次，把 Cookie 交给程序复用。
- **表单登录**（`auth_mode: form`）：适用于仍有「用户名+密码」POST 表单的站点。

重要红线（不可逾越）：
- 本模块【不】实现自动注册、批量建号、验证码破解、撞库。
- 账号由你本人在目标网站完成注册与登录；注册链接见 config/sites.yaml 各站点
  `register_url` 字段（程序只负责展示，不代为点击/填充）。
- 程序仅使用你自己提供的凭据/Cookie 做一次正常登录，拿到会话后访问受限页。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from requests.cookies import RequestsCookieJar, create_cookie

from .credentials import get_cookie_meta, get_cookies, get_credential

logger = logging.getLogger(__name__)


class LoginError(Exception):
    pass


def _site_key(site_cfg: dict) -> str:
    return site_cfg.get("_key") or site_cfg.get("name", "")


def _base_host(site_cfg: dict) -> str:
    base = site_cfg.get("base_url") or ""
    return urlparse(base).hostname or ""


def print_register_hint(site_cfg: dict) -> None:
    reg = site_cfg.get("register_url")
    name = site_cfg.get("name", "")
    key = _site_key(site_cfg)
    print(
        f"\n[需人工配合] 站点「{name}」要求登录后访问受限内容。\n"
        f"  请在本机浏览器打开并完成登录：{reg or site_cfg.get('base_url', '')}\n"
        f"  方式 A（推荐，扫码/验证码登录的站点走这条）：\n"
        f"    用工具导出该站 Cookie 后运行：\n"
        f"      python main.py credentials add-cookie {key} --file cookies.json\n"
        f"  方式 B（仅当该站仍支持用户密码表单登录）：\n"
        f"      python main.py credentials add {key} --username <账号> --password <密码>\n"
        f"  此后程序即可用你的凭据/Cookie 抓取受限页面。\n"
    )


# --------------------------------------------------------------------------- #
# Cookie 注入
# --------------------------------------------------------------------------- #
def _cookie_expired(c: dict) -> bool:
    exp = c.get("expires")
    if exp in (None, "", 0, -1):
        return False
    try:
        return float(exp) < datetime.now(tz=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return False


def inject_cookies(session, site_cfg: dict, strict_domain: bool = True) -> int:
    """把本地加密存储的 Cookie 注入会话，返回注入条数。

    strict_domain=True 时，只注入 domain 属于该站点 base_url 主机的 Cookie，
    避免把无关域的 Cookie 一起发出去。
    """
    key = _site_key(site_cfg)
    cookies = get_cookies(key)
    if not cookies:
        return 0

    host = _base_host(site_cfg)
    jar = RequestsCookieJar()
    skipped_expired = skipped_domain = 0

    for c in cookies:
        if _cookie_expired(c):
            skipped_expired += 1
            continue
        dom = (c.get("domain") or host or "").strip()
        bare = dom.lstrip(".")
        if strict_domain and host and bare and not (host == bare or host.endswith("." + bare)):
            skipped_domain += 1
            continue
        try:
            ck = create_cookie(
                name=c["name"], value=c["value"],
                domain=dom or None, path=c.get("path") or "/",
                secure=bool(c.get("secure")),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("注入 Cookie 失败(%s): %s", c.get("name"), e)
            continue
        jar.set_cookie(ck)

    if skipped_expired or skipped_domain:
        logger.info("Cookie 过滤：过期 %d 条、域不匹配 %d 条", skipped_expired, skipped_domain)

    # 关键：服务端请求需与登录时使用同一个 User-Agent，否则部分站点会拒绝该登录态
    meta = get_cookie_meta(key)
    ua = meta.get("user_agent")
    if ua:
        session.session.headers["User-Agent"] = ua

    session.session.cookies.update(jar)
    return len(jar)


def verify_login(session, site_cfg: dict) -> tuple[bool | None, str]:
    """按站点配置的校验规则判断当前会话是否处于登录态。

    返回 (verdict, message)：True=已登录，False=未登录，None=无法判定（放行）。
    支持配置：
      login:
        verify:
          url: https://www.tgb.cn/          # 用于判定的页面
          must_contain: ["退出"]             # 登录后才会出现的字样
          must_not_contain: ["扫码登录"]      # 登录后应消失的字样
    """
    verify = (site_cfg.get("login") or {}).get("verify") or {}
    url = verify.get("url") or site_cfg.get("base_url")
    if not url:
        return None, "未配置校验地址"
    try:
        r = session.get(url)
    except Exception as e:  # noqa: BLE001
        return None, f"校验请求失败：{str(e)[:120]}"
    if r.status_code >= 400:
        return False, f"校验页返回 HTTP {r.status_code}"

    text = r.text or ""
    must_not = [s for s in (verify.get("must_not_contain") or []) if s]
    must = [s for s in (verify.get("must_contain") or []) if s]

    # ① 命中「未登录标志」→ 明确未登录
    for s in must_not:
        if s in text:
            return False, f"页面仍出现未登录标志「{s}」"
    # ② 配了「已登录标志」→ 必须至少命中一个
    if must:
        hit = [s for s in must if s in text]
        if not hit:
            return False, f"页面未出现已登录标志（期望其一：{'、'.join(must)}）"
        return True, f"命中已登录标志「{hit[0]}」"
    # ③ 只配了「未登录标志」且已全部消失 → 视为已登录（负面证据成立）
    if must_not:
        return True, f"未登录标志已消失（{'、'.join(must_not)}）"
    return None, "已抓取校验页，但未配置判定标志（放行）"


# --------------------------------------------------------------------------- #
# 表单登录（旧路径，保留给仍支持密码登录的站点）
# --------------------------------------------------------------------------- #
def generic_login(session, site_cfg: dict) -> None:
    """通用表单登录。site_cfg['login'] 需含 login_url / method / form 字段。"""
    login_cfg = site_cfg.get("login")
    if not login_cfg:
        raise LoginError(f"站点 {site_cfg.get('name')} 未配置 login 段")

    creds = get_credential(_site_key(site_cfg))
    if not creds:
        print_register_hint(site_cfg)
        raise LoginError(
            f"未找到 {site_cfg.get('name')} 的账号密码凭据。"
            f"若该站为扫码/验证码登录，请改用 credentials add-cookie。"
        )

    login_url = login_cfg.get("login_url")
    if not login_url:
        raise LoginError(f"站点 {site_cfg.get('name')} 未配置 login.login_url")
    method = login_cfg.get("method", "POST").upper()
    form = login_cfg.get("form", {})

    payload: dict = {}
    csrf_field = form.get("csrf_field")
    if csrf_field:
        try:
            page = session.get(login_url, timeout=session.timeout)
            soup = BeautifulSoup(page.text, "lxml")
            token = soup.find("input", {"name": csrf_field})
            if token and token.get("value"):
                payload[csrf_field] = token["value"]
        except Exception as e:  # noqa: BLE001
            logger.warning("CSRF 抓取失败: %s", e)

    payload[form.get("username_field", "username")] = creds["username"]
    payload[form.get("password_field", "password")] = creds["password"]
    payload.update(form.get("extra_fields", {}))

    resp = session.request(
        method, login_url, data=payload,
        timeout=session.timeout, allow_redirects=True,
    )
    if resp.status_code >= 400:
        raise LoginError(f"登录请求失败: HTTP {resp.status_code}")

    verdict = _looks_logged_in(resp, site_cfg)
    if verdict is False:
        raise LoginError(
            "登录可能未成功（仍停留在登录页 / 未拿到会话标识）。\n"
            "请确认：① 账号密码正确；② config/sites.yaml 中该站点的 login 端点与表单字段正确；"
            "③ 该站点是否需要额外验证（短信/滑块）——如需，本程序不支持表单登录，"
            "请改用 Cookie 注入模式：credentials add-cookie。"
        )
    logger.info("登录成功: %s (用户: %s)", site_cfg.get("name"), creds["username"])


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
def ensure_login(session, site_cfg: dict, force_cookie: bool | None = None) -> str:
    """按站点配置完成登录，返回实际使用的方式："cookie" / "form"。

    优先级：Cookie 注入 → 表单登录。Cookie 存在但校验明确判定未登录时，
    若该站也没有账号密码，则直接抛错（提示重新导出 Cookie）。
    """
    key = _site_key(site_cfg)
    login_cfg = site_cfg.get("login") or {}
    mode = (login_cfg.get("auth_mode") or ("cookie" if not get_credential(key) else "form")).lower()
    use_cookie = (mode == "cookie") if force_cookie is None else bool(force_cookie)

    if use_cookie and get_cookies(key):
        n = inject_cookies(session, site_cfg)
        if n:
            verdict, msg = verify_login(session, site_cfg)
            if verdict is not False:
                logger.info("Cookie 注入成功: %s（%d 条，%s）", site_cfg.get("name"), n, msg)
                return "cookie"
            logger.warning("Cookie 校验未通过: %s —— %s", site_cfg.get("name"), msg)
            if not get_credential(key):
                meta = get_cookie_meta(key)
                raise LoginError(
                    f"已存 Cookie 但校验判定未登录（{msg}）。"
                    f"Cookie 存于 {meta.get('saved_at') or '未知时间'}，可能已过期。"
                    f"请重新登录后再次执行 credentials add-cookie {key}。"
                )
        else:
            if not get_credential(key):
                raise LoginError(
                    f"已存 Cookie 但全部过期或域不匹配，无可用条目。"
                    f"请重新登录后再次执行 credentials add-cookie {key}。"
                )

    if not login_cfg:
        raise LoginError(f"站点 {site_cfg.get('name')} 未配置 login 段")
    if not login_cfg.get("login_url"):
        print_register_hint(site_cfg)
        raise LoginError(
            f"站点 {site_cfg.get('name')} 未配置可用的表单登录地址（login_url），"
            f"且本机无有效 Cookie。请执行 credentials add-cookie。"
        )
    generic_login(session, site_cfg)
    return "form"


def _looks_logged_in(resp, site_cfg) -> bool | None:
    """返回 None 表示无法判定（放行）；False 表示明确失败。"""
    marker = (site_cfg.get("login") or {}).get("success_marker")
    if not marker:
        return None
    return marker in resp.text
