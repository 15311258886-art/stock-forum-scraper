"""扩容候选源可达性探测（第二轮）——找还能加进注册表的外媒/科技/科研/亚太媒体。"""
from __future__ import annotations

import concurrent.futures as cf
import ssl
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TARGETS: list[tuple[str, str, str]] = [
    # 英文科技媒体
    ("科技媒体", "TechCrunch", "https://techcrunch.com/feed/"),
    ("科技媒体", "The Verge", "https://www.theverge.com/rss/index.xml"),
    ("科技媒体", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("科技媒体", "Engadget", "https://www.engadget.com/rss.xml"),
    ("科技媒体", "Wired", "https://www.wired.com/feed/rss"),
    ("科技媒体", "MIT Tech Review", "https://www.technologyreview.com/feed/"),
    ("科技媒体", "VentureBeat", "https://venturebeat.com/feed/"),
    ("科技媒体", "ZDNet", "https://www.zdnet.com/news/rss.xml"),
    ("科技媒体", "Slashdot", "https://rss.slashdot.org/Slashdot/slashdotMain"),
    ("科技媒体", "The Register", "https://www.theregister.com/headlines.atom"),

    # 英文财经媒体
    ("财经媒体", "CNBC-Economy", "https://www.cnbc.com/id/10001147/device/rss/rss.html"),
    ("财经媒体", "CNBC-Technology", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("财经媒体", "Fortune", "https://fortune.com/feed/"),
    ("财经媒体", "Forbes-Business", "https://www.forbes.com/business/feed/"),
    ("财经媒体", "BusinessInsider", "https://www.businessinsider.com/rss"),
    ("财经媒体", "The Economist", "https://www.economist.com/finance-and-economics/rss.xml"),
    ("财经媒体", "Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss"),
    ("财经媒体", "Barron's", "https://www.barrons.com/feed"),
    ("财经媒体", "Investopedia", "https://www.investopedia.com/feedbuilder/feed/getfeed?feedName=rss_headline"),
    ("财经媒体", "Nasdaq News", "https://www.nasdaq.com/feed/rssoutbound?category=Markets"),

    # 科研 / 学术（用户重点四类之一）
    ("科研学术", "Nature", "https://www.nature.com/nature.rss"),
    ("科研学术", "Science News", "https://www.science.org/rss/news_current.xml"),
    ("科研学术", "Phys.org", "https://phys.org/rss-feed/"),
    ("科研学术", "EurekAlert", "https://www.eurekalert.org/rss.xml"),
    ("科研学术", "ScienceDaily", "https://www.sciencedaily.com/rss/all.xml"),
    ("科研学术", "Live Science", "https://www.livescience.com/feeds/all"),
    ("科研学术", "New Scientist", "https://www.newscientist.com/feed/home/"),

    # 亚太视角（新马港台）
    ("亚太媒体", "CNA新加坡", "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml"),
    ("亚太媒体", "StraitsTimes", "https://www.straitstimes.com/news/world/rss.xml"),
    ("亚太媒体", "中央社台湾", "https://www.cna.com.tw/rss/aall.aspx"),
    ("亚太媒体", "经济日报台湾", "https://money.udn.com/rssfeed/news/1001/5591"),
    ("亚太媒体", "香港01", "https://www.hk01.com/rss"),
    ("亚太媒体", "香港经济日报", "https://www.hket.com/rss"),
    ("亚太媒体", "BusinessTimes SG", "https://www.businesstimes.com.sg/rss"),
    ("亚太媒体", "Nikkei RSS", "https://asia.nikkei.com/rss/feed/nar"),

    # 中文财经（国内可达，作交叉比对）
    ("中文财经", "华尔街见闻", "https://wallstreetcn.com/rss.xml"),
    ("中文财经", "财联社", "https://www.cls.cn/rss"),
    ("中文财经", "新浪财经", "https://rss.sina.com.cn/finance/stock1.xml"),
]


def probe(t: tuple[str, str, str], timeout: float = 6.0):
    g, name, url = t
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            body = r.read(4096)
            n = body.count(b"<item") + body.count(b"<entry")
            return (g, name, r.status, round(time.time() - t0, 2), "OK", n)
    except urllib.error.HTTPError as e:
        return (g, name, e.code, round(time.time() - t0, 2), "HTTPError", 0)
    except Exception as e:  # noqa: BLE001
        return (g, name, 0, round(time.time() - t0, 2), type(e).__name__, 0)


def main() -> None:
    print("=" * 80)
    print("扩容候选源探测（并发 12，超时 6s）")
    print("=" * 80)
    res = []
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(probe, TARGETS):
            res.append(r)
    for g in ("科技媒体", "财经媒体", "科研学术", "亚太媒体", "中文财经"):
        rows = [r for r in res if r[0] == g]
        if not rows:
            continue
        print(f"\n【{g}】")
        for _, name, code, sec, note, n in rows:
            flag = "✅" if code == 200 else "❌"
            print(f"  {flag} {name:<18} {code:>4} {sec:>6.2f}s  item≈{n:<4} {note}")
    ok = [r for r in res if r[2] == 200]
    print("\n" + "=" * 80)
    print(f"可达 {len(ok)}/{len(res)}：", "、".join(r[1] for r in ok))


if __name__ == "__main__":
    main()
