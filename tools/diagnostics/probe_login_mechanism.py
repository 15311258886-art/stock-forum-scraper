# -*- coding: utf-8 -*-
"""判定淘股吧登录机制：loginStatus / tgbuser 到底是不是登录凭据。

做三件事：
 A. 受限页探测：匿名 vs 带 tgbuser 访问「我的关注」等等待登录页面，比较响应差异
 B. 抓首页引用的 JS，搜索 loginStatus / tgbuser / qrcode 的写入逻辑
产出：tools/_login_mechanism.json + 屏幕摘要
"""
import json
import os
import re
import sys
from urllib.parse import urljoin

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://www.tgb.cn/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# 待登录才能看的受限页（未登录时应被挡）
GATED = [
    "https://www.tgb.cn/user/getMoreListAction",   # 「我的关注」
    "https://www.tgb.cn/user/getSelfInfo",         # 我的信息（猜测）
    "https://www.tgb.cn/user/center",              # 个人中心（猜测）
]
OUT_MARKERS = ["hd_unlogin", "登录/注册", "请登录", "未登录", "登录后"]


def sess(cookies=None):
    s = requests.Session()
    s.headers.update(HEADERS)
    for k, v in (cookies or {}).items():
        s.cookies.set(k, v, domain=".tgb.cn", path="/")
    return s


def probe_gated(label, cookies):
    out = []
    s = sess(cookies)
    for url in GATED:
        try:
            r = s.get(url, timeout=20, allow_redirects=True)
            text = r.text or ""
            out.append({
                "url": url, "status": r.status_code, "final_url": r.url,
                "len": len(text),
                "markers": [m for m in OUT_MARKERS if m in text],
                "looks_gated": any(m in text for m in OUT_MARKERS),
            })
        except Exception as e:
            out.append({"url": url, "error": str(e)[:120]})
    return {"label": label, "results": out}


def probe_js():
    s = sess()
    r = s.get(BASE, timeout=25)
    html = r.text
    srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
    srcs = [urljoin(BASE, u) for u in srcs if not u.startswith("data:")]
    findings = []
    for u in srcs[:40]:
        try:
            jr = s.get(u, timeout=20)
            js = jr.text or ""
        except Exception:
            continue
        for kw in ["loginStatus", "tgbuser", "tfwpwd", "qrcode"]:
            for m in re.finditer(kw, js):
                a = max(0, m.start() - 110)
                snippet = js[a:m.start() + 130].replace("\n", " ")
                findings.append({"js": u, "kw": kw, "snippet": snippet})
                break   # 每个 js 每个关键词只留第一条
    return {"script_count": len(srcs), "js_urls": srcs, "findings": findings}


def main():
    cookies = {"tgbuser": "15299129", "loginStatus": "qrcode"}
    rep = {
        "gated_anonymous": probe_gated("匿名（无 Cookie）", None),
        "gated_with_tgbuser": probe_gated("仅带 tgbuser=15299129 + loginStatus=qrcode", cookies),
        "js_scan": probe_js(),
    }
    with open(os.path.join(HERE, "_login_mechanism.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)

    lines = ["=" * 70]
    for key in ("gated_anonymous", "gated_with_tgbuser"):
        g = rep[key]
        lines.append("【受限页探测】%s" % g["label"])
        for x in g["results"]:
            if "error" in x:
                lines.append("  %-52s ERROR %s" % (x["url"], x["error"]))
            else:
                lines.append("  %-52s HTTP%s len=%-6s 未登录标志=%s"
                             % (x["url"], x["status"], x["len"], x["markers"] or "无"))
        lines.append("")
    js = rep["js_scan"]
    lines.append("【JS 扫描】脚本数=%d，命中片段=%d" % (js["script_count"], len(js["findings"])))
    for f in js["findings"][:25]:
        lines.append("  [%s] %s" % (f["kw"], f["snippet"][:200]))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
