"""探测：联合早报栏目页真实链接 + 国内三源（东财/同花顺/雪球）无条目的原因。"""
from __future__ import annotations

import re
import ssl
import urllib.request
from collections import Counter

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def get(url: str, timeout: float = 12.0, headers: dict | None = None):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout, context=CTX).read()


def zaobao_pages() -> None:
    for path in ("/news/sea", "/news/singapore", "/realtime/world", "/news/malaysia"):
        try:
            html = get("https://www.zaobao.com" + path).decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            print(f"  {path} ✗ {type(e).__name__}")
            continue
        hrefs = re.findall(r'href="(/news/[a-z_]+/story\d{8}-\d+)[^"]*"', html)
        print(f"  {path} len={len(html)} 文章链接={len(hrefs)} 栏目={Counter(h.split('/')[2] for h in hrefs).most_common(6)}")


def eastmoney() -> None:
    try:
        html = get("https://guba.eastmoney.com/default,0_1.html").decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {type(e).__name__}: {str(e)[:100]}")
        return
    print(f"  HTTP ok len={len(html)}")
    for pat in (r'href="(https://guba\.eastmoney\.com/news,[^"]+)"[^>]*title="(?P<t>[^"]{6,80})"',
                r'href="(/news,[^"]+)"[^>]*title="(?P<t>[^"]{6,80})"',
                r'title="([^"]{8,60})"'):
        ms = re.findall(pat, html)
        print(f"    {pat[:52]}… → {len(ms)}")
        if ms:
            for x in ms[:3]:
                print("        ", str(x)[:90])
            return


def ths() -> None:
    try:
        html = get("https://www.10jqka.com.cn/").decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {type(e).__name__}: {str(e)[:100]}")
        return
    print(f"  HTTP ok len={len(html)}")
    hosts = Counter(re.findall(r'href="https?://([^/"]+)/', html)).most_common(8)
    print("    链接域名分布:", hosts)
    for pat in (r'href="(https?://news\.10jqka\.com\.cn/[^"]+)"[^>]*>([^<]{8,80})</a>',
                r'href="(/[^"]+)"[^>]*>([^<]{10,80})</a>'):
        ms = re.findall(pat, html)
        print(f"    {pat[:48]}… → {len(ms)}")
        for x in ms[:3]:
            print("        ", str(x)[:90])
        if ms:
            return


def xueqiu() -> None:
    url = "https://xueqiu.com/statuses/hot/listV2.json?since_id=-1&max_id=-1&size=20"
    try:
        raw = get(url, headers={"Referer": "https://xueqiu.com/", "Accept": "application/json"})
        print(f"  HTTP ok len={len(raw)} 前 200 字节: {raw[:200].decode('utf-8','ignore')}")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {type(e).__name__}: {str(e)[:140]}")


print("=" * 78)
print("① 联合早报栏目页")
print("=" * 78)
zaobao_pages()
print("\n" + "=" * 78)
print("② 东方财富股吧")
print("=" * 78)
eastmoney()
print("\n" + "=" * 78)
print("③ 同花顺")
print("=" * 78)
ths()
print("\n" + "=" * 78)
print("④ 雪球热帖接口")
print("=" * 78)
xueqiu()
