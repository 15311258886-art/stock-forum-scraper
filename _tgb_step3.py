# -*- coding: utf-8 -*-
"""注入新 Cookie 后做真实验证：注入条数 / verify_login 判定 / 匿名对照。"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from scraper.config import get_site, load_config          # noqa: E402
from scraper.credentials import get_cookie_meta, get_cookies  # noqa: E402
from scraper.session import RateLimitedSession           # noqa: E402

OUT_PATH = os.path.join(HERE, "_tgb_step3.txt")
BUF: list[str] = []


def say(s: str = "") -> None:
    BUF.append(str(s))


def probe(label: str, sess: RateLimitedSession, url: str) -> None:
    t0 = time.perf_counter()
    try:
        r = sess.get(url)
    except Exception as e:  # noqa: BLE001
        say(f"  [{label}] 请求异常 {type(e).__name__}: {str(e)[:120]}")
        return
    ms = int((time.perf_counter() - t0) * 1000)
    txt = r.text or ""
    hits = {k: txt.count(k) for k in
            ("个人中心", "超级动态", "hd_unlogin", "扫码登录", "超话", "我的关注")}
    say(f"  [{label}] HTTP {r.status_code}  {ms}ms  {len(txt)}B")
    say(f"      关键词: " + "  ".join(f"{k}={v}" for k, v in hits.items()))


def main() -> int:
    cfg_path = os.path.join(HERE, "config", "sites.yaml")
    cfg = get_site(load_config(cfg_path), "taoguba")
    cfg["_key"] = "taoguba"
    verify_url = cfg["login"]["verify"]["url"]

    meta = get_cookie_meta("taoguba")
    say(f"=== 存储状态 ===  Cookie {meta['count']} 条，保存于 {meta['saved_at']}")
    say(f"    meta: {meta}")

    sess = RateLimitedSession(
        user_agent=cfg["user_agent"],
        rate_limit_per_min=cfg["rate_limit_per_min"],
        timeout=cfg["timeout"],
        respect_robots=False,
    )
    cookies = get_cookies("taoguba")
    say(f"\n=== 注入 ===")
    say(f"    get_cookies() 返回 {len(cookies) if cookies else 0} 条")

    # 1) 先看「没注入」的样子
    say(f"\n=== 匿名（不带 Cookie）访问 {verify_url} ===")
    anon = RateLimitedSession(
        user_agent=cfg["user_agent"],
        rate_limit_per_min=cfg["rate_limit_per_min"],
        timeout=cfg["timeout"],
        respect_robots=False,
    )
    probe("匿名", anon, verify_url)

    # 2) 再注入新 Cookie，按 ensure_login 的真实路径走一遍
    from scraper.auth import ensure_login, inject_cookies, verify_login
    n = inject_cookies(sess, cfg)
    jar = sess.session.cookies
    names = sorted({c.name for c in jar})
    say(f"\n=== 注入新 Cookie（inject_cookies 返回 {n} 条）后 ===")
    say(f"    会话里生效 {len(jar)} 个: {', '.join(names)}")
    hdr = sess.session.headers.get("Cookie") or sess.session.headers.get("cookie") or ""
    say(f"    Cookie 请求头回显 {len(hdr)} 字符")
    say(f"    头片段(脱敏): {hdr[:110]}...")

    verdict, msg = verify_login(sess, cfg)
    say(f"    verify_login => {verdict}   ({msg})")

    probe("带Cookie", sess, verify_url)

    # 3) 首页也看一眼（未被墙的主页，便于确认账号昵称出现）
    probe("带Cookie-首页", sess, "https://www.tgb.cn/")

    # 4) 复用 ensure_login 完整路径（若失败会抛 LoginError）
    try:
        m = ensure_login(sess, cfg)
        say(f"\n    ensure_login => OK  方式={m}")
    except Exception as e:  # noqa: BLE001
        say(f"\n    ensure_login => FAIL {type(e).__name__}: {str(e)[:200]}")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(BUF))
    return 0


if __name__ == "__main__":
    sys.exit(main())
