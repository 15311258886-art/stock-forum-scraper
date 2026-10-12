"""验证可达通道的「实际数据质量」——返回的到底是不是可用新闻/热度。"""
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


def show(title: str, fn) -> None:
    print("\n" + "=" * 74)
    print(f"◆ {title}")
    print("=" * 74)
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 失败: {type(e).__name__}: {str(e)[:160]}")


def t_bing_news() -> None:
    st, b = get("https://cn.bing.com/news/search?q=oil+price&format=rss&count=12")
    txt = b.decode("utf-8", "ignore")
    items = re.findall(r"<item>(.*?)</item>", txt, re.S)[:4]
    print(f"  HTTP {st} | <item> 数: {len(re.findall(r'<item>', txt))}")
    for it in items:
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
        s = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
        print(f"   · [{(s.group(1).strip() if s else '?')}] {(t.group(1).strip()[:70] if t else '?')}")


def t_peertube_search() -> None:
    st, b = get("https://sepiasearch.org/api/v1/search/videos?query=oil%20market&count=5")
    d = json.loads(b.decode("utf-8", "ignore"))
    print(f"  HTTP {st} | total={d.get('total')} 返回={len(d.get('data', []))}")
    for v in d.get("data", [])[:5]:
        print(f"   · [{v.get('publishedAt','')[:10]}] {v.get('name','')[:56]} | 观看={v.get('views')} | 实例={v.get('account',{}).get('host')}")


def t_peertube_trending() -> None:
    st, b = get("https://peertube.tv/api/v1/videos?sort=-trending&count=5&nsfw=false")
    d = json.loads(b.decode("utf-8", "ignore"))
    print(f"  HTTP {st} | total={d.get('total')}")
    for v in d.get("data", [])[:5]:
        print(f"   · [{v.get('publishedAt','')[:10]}] {v.get('name','')[:56]} | 观看={v.get('views')}")


def t_hnrss() -> None:
    st, b = get("https://hnrss.org/frontpage")
    txt = b.decode("utf-8", "ignore")
    items = re.findall(r"<item>(.*?)</item>", txt, re.S)[:4]
    print(f"  HTTP {st} | <item> 数: {len(re.findall(r'<item>', txt))}")
    for it in items:
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
        print(f"   · {(t.group(1).strip()[:74] if t else '?')}")


def t_zaobao() -> None:
    for url in ("https://www.zaobao.com/rss",
                "https://www.zaobao.com/realtime/world/rss.xml",
                "https://www.zaobao.com.sg/realtime/world/rss.xml",
                "https://www.zaobao.com/news/rss"):
        try:
            st, b = get(url, timeout=8)
            txt = b.decode("utf-8", "ignore")
            n = len(re.findall(r"<item[ >]", txt))
            print(f"   {url} -> HTTP {st} | item={n}")
            if n:
                m = re.search(r"<item>(.*?)</item>", txt, re.S)
                t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", m.group(1), re.S)
                print(f"      样例: {t.group(1).strip()[:66] if t else '?'}")
                return
        except Exception as e:  # noqa: BLE001
            print(f"   {url} -> ✗ {type(e).__name__}")


def t_jsdelivr_json() -> None:
    st, b = get("https://cdn.jsdelivr.net/npm/vue@3/package.json")
    d = json.loads(b.decode("utf-8", "ignore"))
    print(f"  HTTP {st} | npm 包 JSON 可取: name={d.get('name')} version={d.get('version')}")
    # 测试能否读 GitHub 仓库的 JSON（数据通道核心能力）
    st2, b2 = get("https://cdn.jsdelivr.net/gh/github/gitignore@main/Global/README.md")
    print(f"  GitHub repo 文件 via jsDelivr: HTTP {st2} | {len(b2)} bytes")


def t_github_trending_api() -> None:
    # 用官方 API 自算 GitHub 趋势（近 7 天新建、star 最多）
    import datetime
    since = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    url = ("https://api.github.com/search/repositories?q=created:>" + since +
           "&sort=stars&order=desc&per_page=5")
    st, b = get(url, timeout=15)
    d = json.loads(b.decode("utf-8", "ignore"))
    print(f"  HTTP {st} | total={d.get('total_count')}")
    for r in d.get("items", [])[:5]:
        print(f"   · ⭐{r.get('stargazers_count')} {r.get('full_name')} | {(r.get('description') or '')[:52]}")


show("Bing 新闻 RSS（搜 'oil price'）", t_bing_news)
show("PeerTube 全网搜索（sepiasearch，搜 'oil market'）", t_peertube_search)
show("PeerTube 趋势榜（peertube.tv trending）", t_peertube_trending)
show("Hacker News 首页（HNRSS）", t_hnrss)
show("联合早报 RSS 候选", t_zaobao)
show("jsDelivr 读 GitHub/npm 文件（数据通道）", t_jsdelivr_json)
show("GitHub 官方 API 自算趋势仓库", t_github_trending_api)
