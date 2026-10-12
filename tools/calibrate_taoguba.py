# -*- coding: utf-8 -*-
"""校准淘股吧列表页选择器：抓真实 HTML，统计候选结构，打印样例
用法：python tools/calibrate_taoguba.py [url ...]
产出：tools/_calib_<n>.html + tools/_calib_report.txt
"""
import os
import re
import sys
from collections import Counter

import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

DEFAULT_URLS = [
    "https://www.tgb.cn/talk/talkSeq/14088",
    "https://www.tgb.cn/",
]

out = []


def log(*a):
    out.append(" ".join(str(x) for x in a))


def analyze(tag, url):
    log("=" * 78)
    log("### %s  %s" % (tag, url))
    try:
        r = requests.get(url, headers=HEADERS, timeout=25, allow_redirects=True)
    except Exception as e:
        log("  请求失败: %s" % str(e)[:150])
        return
    html = r.text or ""
    log("  HTTP=%s final=%s len=%s" % (r.status_code, r.url, len(html)))
    fn = os.path.join(HERE, "_calib_%s.html" % tag)
    with open(fn, "w", encoding="utf-8") as f:
        f.write(html)

    soup = BeautifulSoup(html, "lxml")

    # 1) href 模式统计（把数字折叠成 N）
    pat = Counter()
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if h.startswith("javascript") or h.startswith("#"):
            continue
        pat[re.sub(r"\d+", "N", h)] += 1
    log("  -- 链接模式 TOP15 --")
    for k, v in pat.most_common(15):
        log("    %4d  %s" % (v, k))

    # 2) class 频次
    cls = Counter()
    for t in soup.find_all(True):
        c = t.get("class")
        if c:
            cls[".".join(c) if isinstance(c, list) else str(c)] += 1
    log("  -- class 频次 TOP30 --")
    for k, v in cls.most_common(30):
        log("    %4d  %s" % (v, k))

    # 3) 候选重复容器：出现在 >=5 次的父级结构，取其中带链接的最像列表
    log("  -- 候选列表容器（含 >=5 个链接的重复结构）--")
    best = []
    for t in soup.find_all(["div", "ul", "li", "tr", "section"]):
        links = t.find_all("a", href=True)
        if len(links) < 5:
            continue
        txt = t.get_text(" ", strip=True)
        if len(txt) < 200:
            continue
        best.append((len(links), len(txt), t.name, ".".join(t.get("class") or []) or "(no class)"))
    seen = set()
    for links, tl, name, cl in sorted(best, key=lambda x: -x[1])[:12]:
        key = (name, cl)
        if key in seen:
            continue
        seen.add(key)
        log("    links=%-4d textlen=%-6d <%s class='%s'>" % (links, tl, name, cl))

    # 4) 打印前 6 条含 6 位数字或长文本的链接样例
    log("  -- 帖子样例（前 6 条）--")
    n = 0
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ", strip=True)
        h = a["href"]
        if len(t) < 8 or len(t) > 60:
            continue
        if not re.search(r"/a/|/topic|talkSeq|/t/|/user/", h):
            continue
        log("    [%s] %s" % (t[:44], h[:80]))
        n += 1
        if n >= 6:
            break
    if n == 0:
        log("    （无匹配样例）")


if __name__ == "__main__":
    urls = sys.argv[1:] or DEFAULT_URLS
    for i, u in enumerate(urls):
        analyze("u%d" % i, u)
    txt = "\n".join(out)
    with open(os.path.join(HERE, "_calib_report.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)
