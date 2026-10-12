# -*- coding: utf-8 -*-
"""试点探测：验证「选手个人主页 URL」能否被现有解析器解析。

只抓取、不落生产库；HTML 存到 tools/_probe_out/home/ 供人工核对。
用法：.venv/Scripts/python.exe tools/_probe_players_home.py
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

# 绕过本机全局代理：direct 连接，绝不发往 127.0.0.1 代理
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

CSV = Path(r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\名单B_选手主页URL.csv")
OUT = ROOT / "tools" / "_probe_out" / "home"
OUT.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load_rows():
    with open(CSV, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def pick(rows, site, n=3):
    """优先取 层级=A 或 是否头部=是 的选手。"""
    cand = [r for r in rows if r["来源站点"] == site]
    pref = [r for r in cand if r["层级"] == "A" or r["是否头部"] == "是"]
    chosen, seen = [], set()
    for r in pref + cand:
        u = r["论坛主页URL"]
        if u in seen:
            continue
        seen.add(u)
        chosen.append(r)
        if len(chosen) >= n:
            break
    return chosen


def new_req_session():
    """requests.Session，显式 trust_env=False → 忽略全局代理。"""
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA,
                      "Accept-Language": "zh-CN,zh;q=0.9",
                      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    return s


def probe_taoguba(rows):
    """taoguba 需登录 → 用本地加密 Cookie 搭一个会话。"""
    out = []
    try:
        from scraper.auth import ensure_login
        from scraper.config import get_site, load_config
        from scraper.session import RateLimitedSession

        cfg = get_site(load_config(None), "taoguba")
        cfg["_key"] = "taoguba"
        sess = RateLimitedSession(user_agent=cfg.get("user_agent", UA),
                                  rate_limit_per_min=15, timeout=20, respect_robots=False)
        sess.session.trust_env = False
        method = ensure_login(sess, cfg)
        print(f"[taoguba] 登录方式 = {method}")
        for r in rows:
            url = r["论坛主页URL"]
            try:
                resp = sess.get(url)
                item = {"site": "taoguba", "name": r["选手昵称"], "url": url,
                        "status": resp.status_code, "final": resp.url,
                        "len": len(resp.text or ""), "html": resp.text}
            except Exception as e:  # noqa: BLE001
                item = {"site": "taoguba", "name": r["选手昵称"], "url": url,
                        "status": f"ERR", "final": "", "len": 0, "html": "", "err": str(e)[:160]}
            out.append(item)
    except Exception as e:  # noqa: BLE001
        print(f"[taoguba] 会话/登录失败: {e}")
        for r in rows:
            out.append({"site": "taoguba", "name": r["选手昵称"], "url": r["论坛主页URL"],
                        "status": "LOGIN_ERR", "final": "", "len": 0, "html": "",
                        "err": str(e)[:160]})
    return out


def probe_sina(rows):
    out = []
    for r in rows:
        url = r["论坛主页URL"]
        try:
            s = new_req_session()
            s.headers.update({"Referer": "https://guba.sina.com.cn/"})
            resp = s.get(url, timeout=20)
            if (resp.encoding or "").lower() == "iso-8859-1":
                resp.encoding = "gb18030"
            item = {"site": "sina", "name": r["选手昵称"], "url": url,
                    "status": resp.status_code, "final": resp.url,
                    "len": len(resp.text or ""), "html": resp.text}
        except Exception as e:  # noqa: BLE001
            item = {"site": "sina", "name": r["选手昵称"], "url": url,
                    "status": "ERR", "final": "", "len": 0, "html": "", "err": str(e)[:160]}
        out.append(item)
    return out


def probe_xueqiu(rows):
    out = []
    try:
        from scraper.browser_fetch import browser_get
    except Exception as e:  # noqa: BLE001
        print(f"[xueqiu] browser_fetch 导入失败: {e}")
        browser_get = None
    for r in rows:
        url = r["论坛主页URL"]
        if browser_get is None:
            out.append({"site": "xueqiu", "name": r["选手昵称"], "url": url,
                        "status": "NO_BROWSER", "final": "", "len": 0, "html": ""})
            continue
        try:
            resp = browser_get(url)
            item = {"site": "xueqiu", "name": r["选手昵称"], "url": url,
                    "status": resp.status_code, "final": getattr(resp, "url", url),
                    "len": len(resp.text or ""), "html": resp.text}
        except Exception as e:  # noqa: BLE001
            item = {"site": "xueqiu", "name": r["选手昵称"], "url": url,
                    "status": "ERR", "final": "", "len": 0, "html": "", "err": str(e)[:160]}
        out.append(item)
    return out


def structure(site, html):
    """按各站解析器实际依赖的选择器报告命中数。"""
    soup = BeautifulSoup(html or "", "lxml")
    d = {}
    if site == "taoguba":
        d["p.topic-info"] = len(soup.select("p.topic-info"))
        d["div.topic-new"] = len(soup.select("div.topic-new"))
        d["a[href*='/blog/']"] = len(soup.select("a[href*='/blog/']"))
        d["a[href*='/a/']"] = len(soup.select("a[href*='/a/']"))
        d["title"] = (soup.title.get_text(strip=True) if soup.title else "")[:60]
        d["404"] = ("404" in (soup.title.get_text() if soup.title else "")) or ("页面不存在" in html[:4000])
    elif site == "sina":
        d["a[href*=tid]"] = len(soup.select("a[href*=tid]"))
        d["tr"] = len(soup.find_all("tr"))
        d["a.js_show_hoverCard"] = len(soup.select("a.js_show_hoverCard"))
        d["a[href*='/u/']"] = len(soup.select("a[href*='/u/']"))
        d["title"] = (soup.title.get_text(strip=True) if soup.title else "")[:60]
    elif site == "xueqiu":
        d["article.timeline__item"] = len(soup.select("article.timeline__item"))
        d["a.user-name"] = len(soup.select("a.user-name"))
        d["div.content"] = len(soup.select("div.content"))
        d["title"] = (soup.title.get_text(strip=True) if soup.title else "")[:60]
        d["WAF"] = any(m in (html or "")[:6000] for m in
                       ("aliyun_waf", "renderData", "拖动到最右边", "请按住滑块", "访问验证"))
    return d


def main():
    rows = load_rows()
    print(f"名单共 {len(rows)} 行；站点分布：" +
          ", ".join(f"{s}={sum(1 for r in rows if r['来源站点']==s)}"
                    for s in ("taoguba", "sina", "xueqiu", "jisilu")))
    samples = {
        "taoguba": pick(rows, "taoguba", 3),
        "sina": pick(rows, "sina", 3),
        "xueqiu": pick(rows, "xueqiu", 3),
    }
    for s, rs in samples.items():
        print(f"  [{s}] 抽样: " + " | ".join(f"{r['选手昵称']}({r['层级']}/{r['是否头部']})"
                                             for r in rs))

    results = []
    results += probe_taoguba(samples["taoguba"])
    results += probe_sina(samples["sina"])
    results += probe_xueqiu(samples["xueqiu"])

    print("\n" + "=" * 100)
    print(f"{'站点':<8}{'昵称':<16}{'HTTP':<10}{'长度':<9}{'结构命中'}")
    print("-" * 100)
    for it in results:
        d = structure(it["site"], it.get("html", "")) if it.get("html") else {}
        fname = OUT / f"{it['site']}_{abs(hash(it['url'])) % 100000}.html"
        if it.get("html"):
            fname.write_text(it["html"], encoding="utf-8", errors="replace")
        hit = ""
        K_BLOG = "a[href*='/blog/']"
        K_U = "a[href*='/u/']"
        if it["site"] == "taoguba":
            hit = (f"topic-info={d.get('p.topic-info')} topic-new={d.get('div.topic-new')} "
                   f"blog={d.get(K_BLOG)} 404={d.get('404')} title={d.get('title')}")
        elif it["site"] == "sina":
            hit = (f"tr={d.get('tr')} tid={d.get('a[href*=tid]')} "
                   f"u={d.get(K_U)} title={d.get('title')}")
        elif it["site"] == "xueqiu":
            hit = f"article={d.get('article.timeline__item')} user-name={d.get('a.user-name')} WAF={d.get('WAF')} title={d.get('title')}"
        err = (" ERR=" + it.get("err", "")) if it.get("err") else ""
        print(f"{it['site']:<8}{it['name']:<16}{str(it['status']):<10}{it['len']:<9}{hit}")
        if err:
            print(f"        ↳{err}")
        print(f"        → {it['url']}  (saved {fname.name})")
    print("=" * 100)
    print(f"原始 HTML 目录：{OUT}")


if __name__ == "__main__":
    main()
