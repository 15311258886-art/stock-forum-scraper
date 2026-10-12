#!/usr/bin/env python3
"""境外新闻中继采集（在 GitHub Actions 的境外 runner 上运行）。

背景
----
本机（中国大陆网络）无法直连 YouTube / Reuters / BBC / Al Jazeera / arXiv /
GDELT / Reddit 等信源，但 GitHub Actions 的 runner 位于境外，可正常访问。
本脚本抓取这些源并输出统一 JSON，由 workflow 提交回仓库；
本地采集端（overseas/sources.py 的 relay_json 适配器）再通过
jsDelivr / raw.githubusercontent 读取该 JSON —— 形成“云中继”。

用法
----
    python tools/relay_fetch.py --out data/relay/latest.json --timeout 20

纯标准库实现，无第三方依赖。
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

UA = "Mozilla/5.0 (compatible; overseas-relay/1.0)"
CTX = ssl.create_default_context()

# ---------------------------------------------------------------- 信源清单
# 全部为「本机被墙、境外可达」的源
YOUTUBE_CHANNELS = [
    ("yt_cnbc", "YouTube·CNBC", "UCrp_UI8XtuYfpiqluWLD7Lw"),
    ("yt_bloomberg", "YouTube·Bloomberg TV", "UCIALMKvObZNtJ6AmdCLP7Lg"),
    ("yt_yahoo", "YouTube·Yahoo Finance", "UCEAZeUIeJs0IjQiqTCdVSIg"),
    ("yt_reuters", "YouTube·Reuters", "UChqUTb7kYRX8-EiaN3XFrSQ"),
    ("yt_ft", "YouTube·Financial Times", "UCoUxsWakJucWg46KW5RsvPw"),
    ("yt_economist", "YouTube·The Economist", "UC0p5jTq6XlF_ibP-P5e6Ypw"),
]

RSS_SOURCES = [
    ("reuters_world", "Reuters World", "GLOBAL",
     "https://www.reuters.com/arc/outboundfeeds/rss/category/world/?outputType=xml"),
    ("reuters_markets", "Reuters Markets", "GLOBAL",
     "https://www.reuters.com/arc/outboundfeeds/rss/category/markets/?outputType=xml"),
    ("bbc_world", "BBC World", "GLOBAL", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("bbc_business", "BBC Business", "GLOBAL", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("aljazeera", "Al Jazeera", "GLOBAL", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("nyt_world", "NYT World", "US",
     "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("washpost_world", "Washington Post World", "US",
     "https://feeds.washingtonpost.com/rss/world"),
    ("ft_home", "Financial Times", "GLOBAL", "https://www.ft.com/rss/home"),
    ("bloomberg_markets", "Bloomberg Markets", "US",
     "https://feeds.bloomberg.com/markets/news.rss"),
    ("economist_finance", "The Economist", "GLOBAL",
     "https://www.economist.com/finance-and-economics/rss.xml"),
]

# (key, 展示名, 查询词, 归属主题)
GNEWS_QUERIES = [
    ("gnews_conflict", "GoogleNews·地缘冲突", 'geopolitical conflict OR war OR airstrike OR ceasefire'),
    ("gnews_disaster", "GoogleNews·洪涝灾害", 'flood OR typhoon OR earthquake OR wildfire OR disaster'),
    ("gnews_research", "GoogleNews·科研论文", '"peer-reviewed" study OR "breakthrough research" OR "published in Nature"'),
    ("gnews_food", "GoogleNews·粮食歉收", '"crop failure" OR "poor harvest" OR "food security" OR "grain supply"'),
    ("gnews_oil", "GoogleNews·能源大宗", '"oil price" OR "brent crude" OR opec OR "natural gas"'),
    ("gnews_fed", "GoogleNews·美联储", '"federal reserve" OR "rate decision" OR inflation OR cpi'),
]

ARXIV_QUERIES = [
    ("arxiv_qfin", "arXiv·金融经济论文", "cat:q-fin.GN"),
    ("arxiv_csai", "arXiv·AI 论文", "cat:cs.AI"),
    ("arxiv_econ", "arXiv·经济计量论文", "cat:econ.GN"),
]

GDELT_QUERIES = [
    ("gdelt_conflict", "GDELT·地缘冲突", "geopolitical conflict"),
    ("gdelt_disaster", "GDELT·自然灾害", "flood earthquake typhoon"),
    ("gdelt_supply", "GDELT·供应链扰动", "supply chain disruption"),
]

REDDIT_SUBS = [
    ("reddit_worldnews", "Reddit r/worldnews", "worldnews", "GLOBAL"),
    ("reddit_stocks", "Reddit r/stocks", "stocks", "US"),
    ("reddit_investing", "Reddit r/investing", "investing", "US"),
]


# ---------------------------------------------------------------- HTTP / 工具
def http_get(url: str, timeout: float, headers: dict | None = None) -> bytes:
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()


def strip_html(s: str) -> str:
    s = re.sub(r"<script[\s\S]*?</script>", " ", s or "", flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def parse_time(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?", raw)
    if m:
        y, mo, d, hh, mm, ss = m.groups()
        return f"{y}-{mo}-{d} {hh}:{mm}:{ss or '00'}"
    return ""


def parse_feed(xml_bytes: bytes) -> list[dict]:
    text = xml_bytes.decode("utf-8", "ignore").lstrip("\ufeff").strip()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out: list[dict] = []
    for e in [x for x in root.iter() if x.tag.split("}")[-1].lower() in ("item", "entry")]:
        title = link = desc = pub = ""
        for ch in e:
            t = ch.tag.split("}")[-1].lower()
            if t == "title" and not title:
                title = "".join(ch.itertext())
            elif t == "link" and not link:
                link = (ch.get("href") or (ch.text or "")).strip()
            elif t in ("description", "summary", "content") and not desc:
                desc = "".join(ch.itertext())
            elif t in ("pubdate", "published", "updated", "date") and not pub:
                pub = "".join(ch.itertext())
        title = strip_html(title.replace("<![CDATA[", "").replace("]]>", ""))
        if not title:
            continue
        out.append({"title": title, "url": link,
                    "summary": strip_html(desc)[:400], "publish_time": parse_time(pub)})
    return out


# ---------------------------------------------------------------- 各源抓取
def fetch_youtube(key: str, name: str, channel_id: str, timeout: float) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    items = parse_feed(http_get(url, timeout))
    for it in items:
        it.update(source_key=key, source_name=name, region="US", lang="en",
                  kind="influencer")
    return items[:20]


def fetch_rss(key: str, name: str, region: str, url: str, timeout: float) -> list[dict]:
    items = parse_feed(http_get(url, timeout))
    for it in items:
        it.update(source_key=key, source_name=name, region=region, lang="en", kind="media")
    return items[:30]


def fetch_gnews(key: str, name: str, query: str, timeout: float) -> list[dict]:
    url = ("https://news.google.com/rss/search?" +
           urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}))
    items = parse_feed(http_get(url, timeout))
    for it in items:
        it.update(source_key=key, source_name=name, region="GLOBAL", lang="en",
                  kind="aggregator")
    return items[:25]


def fetch_arxiv(key: str, name: str, cat: str, timeout: float) -> list[dict]:
    url = ("https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
        "search_query": cat, "sortBy": "submittedDate", "sortOrder": "descending",
        "max_results": 20}))
    text = http_get(url, timeout).decode("utf-8", "ignore")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out: list[dict] = []
    for e in root.findall("a:entry", ns):
        title = strip_html((e.findtext("a:title", "", ns) or "").replace("\n", " "))
        link = e.findtext("a:id", "", ns)
        summary = strip_html(e.findtext("a:summary", "", ns))
        pub = e.findtext("a:published", "", ns)
        if not title:
            continue
        out.append({"source_key": key, "source_name": name, "region": "GLOBAL",
                    "lang": "en", "kind": "research",
                    "title": title, "url": link, "summary": summary[:400],
                    "publish_time": parse_time(pub)})
    return out


def fetch_gdelt(key: str, name: str, query: str, timeout: float) -> list[dict]:
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode({
        "query": query, "mode": "ArtList", "maxrecords": 25,
        "sort": "HybridRel", "format": "json"}))
    data = json.loads(http_get(url, timeout).decode("utf-8", "ignore") or "{}")
    out: list[dict] = []
    for a in data.get("articles", []) or []:
        title = strip_html(a.get("title") or "")
        if not title:
            continue
        out.append({"source_key": key, "source_name": f"{name}·{a.get('domain','')}".rstrip("·"),
                    "region": "GLOBAL", "lang": a.get("language") or "en", "kind": "media",
                    "title": title, "url": a.get("url") or "",
                    "summary": "", "publish_time": parse_time(a.get("seendate") or "")})
    return out


def fetch_reddit(key: str, name: str, sub: str, region: str, timeout: float) -> list[dict]:
    url = f"https://www.reddit.com/r/{sub}/hot.json?limit=20"
    data = json.loads(http_get(url, timeout, {"Accept": "application/json"}).decode("utf-8", "ignore") or "{}")
    out: list[dict] = []
    for ch in (data.get("data", {}) or {}).get("children", []) or []:
        d = ch.get("data", {}) or {}
        title = d.get("title") or ""
        if not title:
            continue
        out.append({"source_key": key, "source_name": name, "region": region,
                    "lang": "en", "kind": "forum", "title": title,
                    "url": "https://www.reddit.com" + (d.get("permalink") or ""),
                    "summary": strip_html(d.get("selftext") or "")[:300],
                    "publish_time": datetime.fromtimestamp(
                        d.get("created_utc", 0), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    if d.get("created_utc") else ""})
    return out


# ---------------------------------------------------------------- 主流程
def build_jobs(timeout: float) -> list[tuple[str, callable]]:
    jobs: list[tuple[str, callable]] = []
    for k, n, cid in YOUTUBE_CHANNELS:
        jobs.append((k, lambda k=k, n=n, cid=cid: fetch_youtube(k, n, cid, timeout)))
    for k, n, rg, url in RSS_SOURCES:
        jobs.append((k, lambda k=k, n=n, rg=rg, url=url: fetch_rss(k, n, rg, url, timeout)))
    for k, n, q in GNEWS_QUERIES:
        jobs.append((k, lambda k=k, n=n, q=q: fetch_gnews(k, n, q, timeout)))
    for k, n, cat in ARXIV_QUERIES:
        jobs.append((k, lambda k=k, n=n, cat=cat: fetch_arxiv(k, n, cat, timeout)))
    for k, n, q in GDELT_QUERIES:
        jobs.append((k, lambda k=k, n=n, q=q: fetch_gdelt(k, n, q, timeout)))
    for k, n, sub, rg in REDDIT_SUBS:
        jobs.append((k, lambda k=k, n=n, sub=sub, rg=rg: fetch_reddit(k, n, sub, rg, timeout)))
    return jobs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/relay/latest.json")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    jobs = build_jobs(args.timeout)
    items: list[dict] = []
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}

    def run(job):
        key, fn = job
        try:
            rows = fn()
            return key, rows, ""
        except Exception as e:  # noqa: BLE001
            return key, [], f"{type(e).__name__}: {str(e)[:120]}"

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for key, rows, err in ex.map(run, jobs):
            if err:
                errors[key] = err
                print(f"  ✗ {key:<20} {err}", file=sys.stderr)
            else:
                counts[key] = len(rows)
                items.extend(rows)
                print(f"  ✓ {key:<20} {len(rows)}")

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": counts,
        "errors": errors,
        "items": items,
    }
    import pathlib
    p = pathlib.Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n总计 {len(items)} 条 → {p}（成功 {len(counts)} 源 / 失败 {len(errors)} 源）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
