"""境外信源可达性批量探测（只读，不改数据）。

用途：为「新闻获取功能扩展数据源」摸清本机真实网络边界——
哪些外媒/视频平台/代理通道能直连，哪些必须走绕行方案。
输出：可达清单 + 耗时 + 失败原因，供选型决策。
"""
from __future__ import annotations

import concurrent.futures as cf
import socket
import ssl
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# (分组, 名称, URL)
TARGETS: list[tuple[str, str, str]] = [
    # ---------- A. 外媒中文版（面向中国读者，通常有可达 CDN）----------
    ("外媒中文", "纽约时报中文", "https://cn.nytimes.com/"),
    ("外媒中文", "华尔街日报中文", "https://cn.wsj.com/"),
    ("外媒中文", "FT中文网", "https://www.ftchinese.com/"),
    ("外媒中文", "FT中文 RSS", "https://www.ftchinese.com/rss/news"),
    ("外媒中文", "德国之声中文", "https://www.dw.com/zh/"),
    ("外媒中文", "德国之声中文RSS", "https://rss.dw.com/rdf/rss-chi-all"),
    ("外媒中文", "VOA中文", "https://www.voachinese.com/"),
    ("外媒中文", "法广中文RSS", "https://www.rfi.fr/cn/rss"),
    ("外媒中文", "联合早报", "https://www.zaobao.com/"),
    ("外媒中文", "路透中文", "https://cn.reuters.com/"),
    ("外媒中文", "BBC中文", "https://www.bbc.com/zhongwen/simp"),
    ("外媒中文", "日经亚洲", "https://asia.nikkei.com/"),

    # ---------- B. 原有可达源复核 ----------
    ("原有源", "CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("原有源", "MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("原有源", "SeekingAlpha", "https://seekingalpha.com/market_currents.xml"),
    ("原有源", "HKEX新闻稿", "https://www.hkex.com.hk/Services/RSS-Feeds/News-Releases?sc_lang=en"),

    # ---------- C. GitHub / CDN 绕行通道 ----------
    ("GH通道", "raw.githubusercontent", "https://raw.githubusercontent.com/github/gitignore/main/README.md"),
    ("GH通道", "jsDelivr-gh", "https://cdn.jsdelivr.net/gh/github/gitignore@main/README.md"),
    ("GH通道", "jsDelivr-fastly", "https://fastly.jsdelivr.net/gh/github/gitignore@main/README.md"),
    ("GH通道", "jsDelivr-gcore", "https://gcore.jsdelivr.net/gh/github/gitignore@main/README.md"),
    ("GH通道", "ghfast.top", "https://ghfast.top/https://raw.githubusercontent.com/github/gitignore/main/README.md"),
    ("GH通道", "ghproxy.net", "https://ghproxy.net/https://raw.githubusercontent.com/github/gitignore/main/README.md"),
    ("GH通道", "gitmirror-raw", "https://raw.gitmirror.com/github/gitignore/main/README.md"),
    ("GH通道", "gitclone", "https://gitclone.com/github.com/github/gitignore"),
    ("GH通道", "api.github.", "https://api.github.com/rate_limit"),
    ("GH通道", "gitee", "https://gitee.com/"),

    # ---------- D. 视频平台（YouTube 替代 / 类似B站）----------
    ("视频", "PeerTube主站", "https://peertube.tv/"),
    ("视频", "PeerTube搜索", "https://sepiasearch.org/api/v1/search/videos?searchTarget=local&query=news&count=2"),
    ("视频", "PeerTube实例表", "https://instances.joinpeertube.org/api/v1/instances?count=2"),
    ("视频", "PeerTube API样本", "https://peertube.tv/api/v1/videos?count=2"),
    ("视频", "Invidious-yewtu", "https://yewtu.be/api/v1/trending?region=US"),
    ("视频", "Invidious-nadeko", "https://inv.nadeko.net/api/v1/trending?region=US"),
    ("视频", "Piped-kavin", "https://pipedapi.kavin.rocks/trending?region=US"),
    ("视频", "Piped-adminforge", "https://pipedapi.adminforge.de/trending?region=US"),
    ("视频", "Rumble", "https://rumble.com/"),
    ("视频", "BitChute", "https://www.bitchute.com/"),
    ("视频", "Vimeo", "https://vimeo.com/"),

    # ---------- E. RSSHub / 聚合器（自建实例，可能可达）----------
    ("聚合", "RSSHub-rssforever", "https://rsshub.rssforever.com/github/trending/daily"),
    ("聚合", "RSSHub-pseudoyu", "https://rsshub.pseudoyu.com/github/trending/daily"),
    ("聚合", "RSSHub-slarker", "https://hub.slarker.me/github/trending/daily"),
    ("聚合", "RSSHub-ktachibana", "https://rsshub.ktachibana.party/github/trending/daily"),
    ("聚合", "HN-Algolia", "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=2"),
    ("聚合", "HNRSS", "https://hnrss.org/frontpage"),
    ("聚合", "Bing新闻", "https://cn.bing.com/news/search?q=oil"),
    ("聚合", "arXiv API", "https://export.arxiv.org/api/query?search_query=all:semiconductor&max_results=2"),

    # ---------- F. 国际通讯社（预期被墙，作对照）----------
    ("被墙对照", "Reuters", "https://www.reuters.com/"),
    ("被墙对照", "BBC英文RSS", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("被墙对照", "AlJazeera RSS", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("被墙对照", "YouTube", "https://www.youtube.com/"),
    ("被墙对照", "Reddit", "https://www.reddit.com/r/worldnews/.json?limit=1"),
    ("被墙对照", "GDELT", "https://api.gdeltproject.org/api/v2/doc/doc?query=oil&format=json"),
]

# 常见本地代理端口（若用户开着 VPN 但未设环境变量）
PROXY_PORTS = [7890, 7897, 7891, 10809, 10808, 1080, 8080, 8889, 20171, 33210]


def probe(target: tuple[str, str, str], timeout: float = 6.0):
    group, name, url = target
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            r.read(1024)
            return (group, name, r.status, round(time.time() - t0, 2), "OK")
    except urllib.error.HTTPError as e:
        return (group, name, e.code, round(time.time() - t0, 2), "HTTPError")
    except Exception as e:  # noqa: BLE001
        return (group, name, 0, round(time.time() - t0, 2), type(e).__name__)


def check_proxy(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def main() -> None:
    print("=" * 78)
    print("境外信源可达性探测（并发 12，超时 6s）—— 0=连接失败 200=可达")
    print("=" * 78)
    results = []
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(probe, TARGETS):
            results.append(r)

    order = ["外媒中文", "原有源", "GH通道", "视频", "聚合", "被墙对照"]
    for g in order:
        rows = [r for r in results if r[0] == g]
        if not rows:
            continue
        print(f"\n【{g}】")
        for _, name, code, sec, note in rows:
            flag = "✅" if code == 200 else ("⚠️ " if 300 <= code < 400 else "❌")
            print(f"  {flag} {name:<20} {code:>4}  {sec:>6.2f}s  {note}")

    ok = [r for r in results if r[2] == 200]
    print("\n" + "=" * 78)
    print(f"可达: {len(ok)}/{len(results)}")
    print("可达清单:", "、".join(r[1] for r in ok))
    print("=" * 78)
    print("\n【本地代理端口扫描】")
    found = [p for p in PROXY_PORTS if check_proxy(p)]
    print("  开放端口:", found if found else "无（未检测到本地代理/VPN 在监听）")
    import os
    env_p = {k: v for k, v in os.environ.items() if "proxy" in k.lower()}
    print("  环境变量代理:", env_p if env_p else "无")


if __name__ == "__main__":
    main()
