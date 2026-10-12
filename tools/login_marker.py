"""登录态判定标志的校准/核对工具。

用法：
  python tools/login_marker.py                 # 未登录基线（不带 Cookie）
  python tools/login_marker.py --with-cookie   # 带本机已存 Cookie

会抓取 config/sites.yaml 中 taoguba 的 base_url，统计候选标志的出现次数，
并把 HTML 落盘到 tools/_probe_<mode>.html 以便对比。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper.auth import inject_cookies  # noqa: E402
from scraper.config import get_site, load_config  # noqa: E402
from scraper.session import RateLimitedSession  # noqa: E402

CANDIDATES = [
    "登录", "注册", "扫码登录", "微信", "退出", "退出登录", "注销",
    "我的", "个人中心", "设置", "消息", "私信", "收藏", "关注",
    "我的主页", "账号", "退出账号", "登录后", "请先登录",
]


def main() -> None:
    use_cookie = "--with-cookie" in sys.argv
    data = load_config(str(ROOT / "config" / "sites.yaml"))
    cfg = get_site(data, "taoguba")
    cfg["_key"] = "taoguba"
    url = (cfg.get("login", {}).get("verify") or {}).get("url") or cfg["base_url"]

    sess = RateLimitedSession(
        user_agent=cfg.get("user_agent", ""),
        rate_limit_per_min=cfg.get("rate_limit_per_min", 15),
        timeout=cfg.get("timeout", 15),
    )
    mode = "with-cookie" if use_cookie else "anonymous"
    if use_cookie:
        n = inject_cookies(sess, cfg)
        print(f"[Cookie] 注入 {n} 条")
    else:
        print("[Cookie] 未注入（未登录基线）")

    r = sess.get(url)
    text = r.text or ""
    print(f"URL={url}\nHTTP={r.status_code}  长度={len(text)}  最终UA={sess.session.headers.get('User-Agent','')[:60]}")

    out = ROOT / "tools" / f"_probe_{mode}.html"
    out.write_text(text, encoding="utf-8")
    print(f"HTML 已落盘: {out}")

    print("\n候选标志出现次数：")
    for s in CANDIDATES:
        c = text.count(s)
        if c:
            print(f"  {s:<8} x{c}")
    hits = [s for s in CANDIDATES if s not in text]
    print("\n未出现：", "、".join(hits) if hits else "（无）")

    title = re.search(r"<title>(.*?)</title>", text, re.S)
    print("title:", (title.group(1).strip()[:80] if title else "(无)"))


if __name__ == "__main__":
    main()
