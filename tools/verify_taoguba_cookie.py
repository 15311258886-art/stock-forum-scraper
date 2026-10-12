# -*- coding: utf-8 -*-
"""淘股吧 Cookie 登录态验证器（独立于主程序，可单独跑）

用法：
    python tools/verify_taoguba_cookie.py                      # 只用匿名（无 Cookie）跑基线
    python tools/verify_taoguba_cookie.py tools/cookies.json   # 用指定 Cookie 文件跑
    python tools/verify_taoguba_cookie.py --header "a=1; b=2"  # 直接贴浏览器请求头里的 Cookie 串

判定口径（2026-09-22 二次修正，比首页判据硬）：
  主判据 = 受限页 https://www.tgb.cn/user/getMoreListAction（「我的关注」，需登录）
      · 未登录 → 302 到 https://sso.tgb.cn/web/login/index?url=...，或正文出现未登录字样
      · 已登录 → 直接返回关注列表
  辅助判据 = 首页正文是否出现 hd_unlogin / 登录/注册
输出：tools/_verify_result.json + 屏幕摘要
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
HOME_URL = "https://www.tgb.cn/"
GATED_URL = "https://www.tgb.cn/user/getMoreListAction"   # 「我的关注」
SSO_HINT = "sso.tgb.cn/web/login"
OUT_MARKERS = ["hd_unlogin", "登录/注册"]
IDENTITY_COOKIE_HINTS = ["tgbuser", "tfwpwd", "userid", "uid", "nick", "ssomod_itna",
                         "loginStatus", "ssoToken", "token"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def load_cookies(src: str):
    """支持三种输入：capture json / {name:value} json / 'a=1; b=2' 文本"""
    if os.path.isfile(src):
        with open(src, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    else:
        raw = src.strip()

    if raw.startswith("{"):
        try:
            j = json.loads(raw)
        except Exception:
            j = None
        if isinstance(j, dict):
            if "cookies" in j and isinstance(j["cookies"], list):   # capture 格式
                out = [{"name": c.get("name"), "value": c.get("value"),
                        "domain": c.get("domain", ".tgb.cn"), "path": c.get("path", "/")}
                       for c in j["cookies"]]
                return out, j.get("user_agent") or UA, j.get("captured_at")
            return [{"name": k, "value": v, "domain": ".tgb.cn", "path": "/"} for k, v in j.items()], UA, None

    pairs = []
    for seg in re.split(r"[;\n]", raw):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        pairs.append({"name": k.strip(), "value": v.strip(), "domain": ".tgb.cn", "path": "/"})
    return pairs, UA, None


def _build(cookies, ua):
    jar = requests.cookies.RequestsCookieJar()
    for c in cookies:
        if not c.get("name"):
            continue
        jar.set(c["name"], c.get("value") or "",
                domain=c.get("domain") or None, path=c.get("path") or "/")
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept-Language": "zh-CN,zh;q=0.9",
                      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    s.cookies.update(jar)
    return s


def run(cookies, ua, label):
    s = _build(cookies, ua)
    names = [c["name"] for c in cookies if c.get("name")]
    res = {"label": label, "cookie_count": len(names), "checked_at": datetime.now(timezone.utc).isoformat(),
           "cookie_names": sorted(names),
           "identity_cookie_names_present": [n for n in IDENTITY_COOKIE_HINTS if n in names]}

    # —— 主判据：受限页 ——
    try:
        g = s.get(GATED_URL, timeout=25, allow_redirects=True)
        gtext = g.text or ""
        blocked = (SSO_HINT in g.url) or any(m in gtext for m in OUT_MARKERS)
        res["gated"] = {"url": GATED_URL, "status": g.status_code, "final_url": g.url,
                        "html_len": len(gtext), "blocked": blocked,
                        "markers_hit": [m for m in OUT_MARKERS if m in gtext]}
    except Exception as e:
        res["gated"] = {"url": GATED_URL, "error": str(e)[:150]}
        blocked = None

    # —— 辅助判据：首页 ——
    try:
        h = s.get(HOME_URL, timeout=25, allow_redirects=True)
        htext = h.text or ""
        hits = [m for m in OUT_MARKERS if m in htext]
        res["home"] = {"status": h.status_code, "html_len": len(htext),
                       "unlogin_markers_hit": hits,
                       "verdict": "LOGGED_IN" if (not hits and len(htext) > 5000) else
                                  ("NOT_LOGGED_IN" if len(htext) > 5000 else "UNKNOWN")}
    except Exception as e:
        res["home"] = {"error": str(e)[:150]}

    if blocked is None:
        res.update(verdict="ERROR", message="受限页请求失败，无法判定")
    elif blocked:
        res.update(verdict="NOT_LOGGED_IN",
                   message="受限页被挡：%s" % ("跳转 SSO 登录页" if SSO_HINT in res["gated"].get("final_url", "")
                                             else "命中未登录字样 %s" % res["gated"].get("markers_hit")))
    else:
        res.update(verdict="LOGGED_IN", message="受限页正常返回，未出现登录拦截标志 → 判定已登录")
    return res


def main():
    args = sys.argv[1:]
    results = []
    if args and args[0] == "--header":
        cookies, ua, _ = load_cookies(args[1])
        results.append(run(cookies, ua, "提供的 Cookie 串"))
    elif args:
        cookies, ua, cap = load_cookies(args[0])
        results.append(run(cookies, ua, "文件 %s（采集于 %s）" % (os.path.basename(args[0]), cap or "?")))
    else:
        results.append(run([], UA, "匿名基线（不带任何 Cookie）"))

    with open(os.path.join(HERE, "_verify_result.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    lines = []
    for r in results:
        lines.append("=" * 70)
        lines.append("【%s】" % r["label"])
        lines.append("  Cookie 条数 : %s" % r.get("cookie_count"))
        g = r.get("gated") or {}
        if "error" in g:
            lines.append("  受限页      : 请求失败 - %s" % g["error"])
        else:
            lines.append("  受限页      : HTTP%s len=%s" % (g.get("status"), g.get("html_len")))
            lines.append("  受限页最终URL: %s" % g.get("final_url"))
            lines.append("  受限页被挡  : %s  (未登录字样=%s)" % (g.get("blocked"), g.get("markers_hit") or "无"))
        h = r.get("home") or {}
        if "error" not in h:
            lines.append("  首页        : HTTP%s len=%s 未登录标志=%s"
                         % (h.get("status"), h.get("html_len"), h.get("unlogin_markers_hit") or "无"))
        lines.append("  ==> 判定    : %s" % r.get("verdict"))
        lines.append("  说明        : %s" % r.get("message"))
        lines.append("  身份类Cookie: %s" % (", ".join(r.get("identity_cookie_names_present") or []) or "无"))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
