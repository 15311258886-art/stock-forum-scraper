"""确认特殊源的条目解析方式（Bing 新闻 / 联合早报 / PeerTube 搜索参数）。"""
from __future__ import annotations

import json
import re
import ssl
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def get(url: str, timeout: float = 12.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.status, r.read()


def show(t: str, fn) -> None:
    print("\n" + "=" * 74)
    print(f"◆ {t}")
    print("=" * 74)
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {type(e).__name__}: {str(e)[:150]}")


def bing_rss_variants() -> None:
    for url in ("https://www.bing.com/news/search?q=oil+price&format=RSS",
                "https://cn.bing.com/news/search?q=oil+price&format=RSS",
                "https://www.bing.com/news/search?q=oil+price&format=rss&count=15"):
        try:
            st, b = get(url, timeout=8)
            txt = b.decode("utf-8", "ignore")
            n = len(re.findall(r"<item[ >]", txt))
            print(f"  HTTP {st} item={n}  {url[:78]}")
            if n:
                m = re.search(r"<item>(.*?)</item>", txt, re.S)
                ti = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", m.group(1), re.S)
                print(f"      样例: {ti.group(1).strip()[:70] if ti else '?'}")
                return
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {type(e).__name__} {url[:60]}")


def bing_html() -> None:
    st, b = get("https://cn.bing.com/news/search?q=oil+price")
    html = b.decode("utf-8", "ignore")
    print(f"  HTTP {st} | 长度={len(html)}")
    # Bing 新闻卡片：<a class="title" href="...">标题</a>
    for pat in (r'<a[^>]+class="title"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                r'<div class="news-card[^"]*"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>'):
        ms = re.findall(pat, html, re.S)
        print(f"  模式命中 {len(ms)} 条: {pat[:40]}...")
        for u, t in ms[:3]:
            print(f"     · {re.sub('<[^>]+>','',t).strip()[:60]}")
        if ms:
            return
    # 兜底：找 data-url
    print("  data-url 命中:", len(re.findall(r'data-url="(https?://[^"]+)"', html)))


def zaobao_html() -> None:
    for url in ("https://www.zaobao.com/", "https://www.zaobao.com/realtime/world"):
        st, b = get(url, timeout=10)
        html = b.decode("utf-8", "ignore")
        ms = re.findall(r'<a[^>]+href="(/(?:news|realtime|forum)/[^"]{6,90})"[^>]*>(.*?)</a>', html, re.S)
        print(f"  {url} HTTP {st} 长度={len(html)} 链接命中={len(ms)}")
        for u, t in ms[:5]:
            txt = re.sub(r"<[^>]+>", "", t).strip()
            if len(txt) > 6:
                print(f"     · {txt[:62]}  ->{u[:40]}")
        if ms:
            return


def peertube_search_index() -> None:
    for q in ("https://sepiasearch.org/api/v1/search/videos?searchTarget=search-index&query=oil+price&count=4&sort=-match",
              "https://sepiasearch.org/api/v1/search/videos?searchTarget=search-index&query=stock+market&count=4&sort=-publishedAt"):
        st, b = get(q, timeout=14)
        d = json.loads(b.decode("utf-8", "ignore"))
        print(f"  HTTP {st} total={d.get('total')}")
        for v in d.get("data", [])[:4]:
            print(f"     · [{v.get('publishedAt','')[:10]}] {v.get('name','')[:52]} | 观看={v.get('views')}")


def peertube_search_trending() -> None:
    q = "https://sepiasearch.org/api/v1/search/videos?searchTarget=search-index&query=finance&count=4&sort=-trending"
    st, b = get(q, timeout=14)
    d = json.loads(b.decode("utf-8", "ignore"))
    print(f"  HTTP {st} total={d.get('total')}")
    for v in d.get("data", [])[:4]:
        print(f"     · [{v.get('publishedAt','')[:10]}] {v.get('name','')[:52]} | 观看={v.get('views')} | {v.get('account',{}).get('host')}")


show("Bing 新闻 RSS 变体重试", bing_rss_variants)
show("Bing 新闻 HTML 结构", bing_html)
show("联合早报 HTML 结构", zaobao_html)
show("PeerTube sepiasearch（search-index + sort=-match）", peertube_search_index)
show("PeerTube sepiasearch（sort=-trending）", peertube_search_trending)
