"""海外数据源采集适配层（美股 / 港股舆情 + 国外新闻驱动热点）。

设计要点
--------
1. **多类型适配器**，统一产出 `NewsItem`：
   - `rss`              标准 RSS 2.0 / Atom（CNBC、MarketWatch、SeekingAlpha、HKEX…）
   - `html`             网页正则抽取（东财股吧、同花顺、AAStocks 等列表页）
   - `youtube_api`      YouTube Data API v3（需 API Key）
   - `reddit_json`      Reddit 公开 JSON（需 VPN）
   - `google_news_rss`  Google News 检索 RSS（需 VPN）
   - `xueqiu_json`      雪球热帖 JSON
2. **诚实状态**：每个源返回 enabled / reachable / requires_key / requires_vpn / note，
   界面据此显示「已接入 / 待激活」，绝不把抓不到的数据当作已抓到。
3. 纯标准库实现（urllib + xml.etree + re），无第三方依赖。
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "overseas_sources.yaml"

import netctx  # noqa: E402  —— 统一出网 TLS 口径（整改 #27）
_SSL = netctx.https_context()  # 整改 #27：默认不校验，按环境变量可开启
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


@dataclass
class NewsItem:
    """统一新闻条目。"""
    source_key: str
    source_name: str
    region: str                 # US / HK / GLOBAL / CN
    lang: str
    kind: str                   # media / blog / official / influencer / forum / aggregator
    title: str
    url: str = ""
    summary: str = ""
    publish_time: str = ""
    market: list[str] = field(default_factory=list)


@dataclass
class SourceResult:
    key: str
    name: str
    region: str
    enabled: bool
    ok: bool = False
    items: list[NewsItem] = field(default_factory=list)
    error: str = ""
    latency: float = 0.0
    requires_key: bool = False
    requires_vpn: bool = False
    note: str = ""
    custom_status: str = ""

    @property
    def status(self) -> str:
        if self.custom_status:
            return self.custom_status
        if self.ok and self.items:
            return "已接入"
        if self.ok and not self.items:
            return "已连通但无条目"
        if self.requires_key:
            return "待激活（需 API Key）"
        if self.requires_vpn:
            return "待激活（需 VPN）"
        if not self.enabled:
            return "未启用"
        return "抓取失败"

    def as_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "region": self.region,
            "enabled": self.enabled, "ok": self.ok, "count": len(self.items),
            "error": self.error, "latency": round(self.latency, 2),
            "status": self.status, "requires_key": self.requires_key,
            "requires_vpn": self.requires_vpn, "note": self.note,
        }


# ---------------------------------------------------------------- 配置加载
def load_overseas_config(path: Path | None = None) -> dict:
    """读取 config/overseas_sources.yaml。无 PyYAML 时回退到极简解析（仅本项目字段）。"""
    p = path or CONFIG_PATH
    if not p.exists():
        return {"sources": [], "region_weights": {}, "topics": {}, "defaults": {}}
    text = p.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001 —— 无 yaml 时用内置解析兜底
        data = _mini_yaml(text)
    data.setdefault("sources", [])
    data.setdefault("region_weights", {"US": 1.0, "HK": 0.85, "GLOBAL": 0.8, "CN": 0.45})
    data.setdefault("topics", {})
    return data


def _mini_yaml(text: str) -> dict:
    """极简 YAML 兜底：仅支持 sources 列表与 region_weights/topics 的浅层映射。"""
    out: dict = {"sources": [], "region_weights": {}, "topics": {}}
    cur: dict | None = None
    section = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^region_weights:", line):
            section, cur = "rw", None
            continue
        if re.match(r"^topics:", line):
            section, cur = "topics", None
            continue
        if re.match(r"^defaults:", line):
            section, cur = "defaults", None
            continue
        if re.match(r"^sources:", line):
            section, cur = "sources", None
            continue
        if section == "sources":
            m = re.match(r"^  - key:\s*(\S+)", line)
            if m:
                cur = {"key": m.group(1)}
                out["sources"].append(cur)
                continue
            m = re.match(r"^    ([a-z_]+):\s*(.*)$", line)
            if m and cur is not None:
                k, v = m.group(1), m.group(2).strip()
                if v.startswith("[") and v.endswith("]"):
                    cur[k] = [x.strip().strip('"\'') for x in v[1:-1].split(",") if x.strip()]
                elif v.lower() in ("true", "false"):
                    cur[k] = v.lower() == "true"
                elif v and not v.startswith("#"):
                    cur[k] = v.strip('"\'')
        elif section == "rw":
            m = re.match(r"^  ([A-Z]+):\s*([\d.]+)", line)
            if m:
                out["region_weights"][m.group(1)] = float(m.group(2))
        elif section == "topics":
            m = re.match(r"^  ([^\s:]+):\s*\[(.*)\]\s*$", line)
            if m:
                out["topics"][m.group(1)] = [
                    x.strip().strip('"\'') for x in m.group(2).split(",") if x.strip()]
    return out


def sources_of(data: dict, enabled_only: bool = False) -> list[dict]:
    srcs = list(data.get("sources") or [])
    return [s for s in srcs if s.get("enabled")] if enabled_only else srcs


# ---------------------------------------------------------------- HTTP
def http_get(url: str, timeout: int = 12, headers: dict | None = None) -> bytes:
    h = {"User-Agent": DEFAULT_UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return r.read()


def _parse_time(raw: str) -> str:
    """把 RFC822 / ISO8601 / 北京时间串统一为 'YYYY-MM-DD HH:MM:SS'。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?", raw)
    if m:
        y, mo, d, hh, mm, ss = m.groups()
        return f"{y}-{mo}-{d} {hh}:{mm}:{ss or '00'}"
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", raw)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d} 00:00:00"
    return ""


def _strip_html(s: str) -> str:
    s = re.sub(r"<script[\s\S]*?</script>", " ", s or "", flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
         .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", s).strip()


def _unescape_cdata(s: str) -> str:
    return (s or "").replace("<![CDATA[", "").replace("]]>", "").strip()


# 促销 / 导流类噪声条目（Wired、TechCrunch 等 RSS 常混入优惠券与推广文）
_AD_JUNK_RE = re.compile(
    r"(promo code|coupon code|discount code|\bsave \d+%|\b\d+%\s*off\b|deal of the day|"
    r"shop now|buy now|free trial|sign up (now|today)|subscribe (now|today)|"
    r"优惠券|折扣码|限时优惠|立即购买|点击领取)",
    re.I)


def _is_junk(title: str) -> bool:
    """判断条目是否为促销/导流噪声，应丢弃。"""
    return bool(_AD_JUNK_RE.search(title or ""))


# ---------------------------------------------------------------- RSS / Atom
def parse_feed(xml_bytes: bytes, cfg: dict) -> list[NewsItem]:
    """解析 RSS 2.0 或 Atom，返回 NewsItem 列表。"""
    text = xml_bytes.decode("utf-8", "ignore").lstrip("\ufeff").strip()
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return items

    def tag_of(el) -> str:
        return el.tag.split("}")[-1].lower()

    entries = [e for e in root.iter() if tag_of(e) in ("item", "entry")]
    for e in entries:
        title = link = desc = pub = ""
        for ch in e:
            t = tag_of(ch)
            if t == "title" and not title:
                title = _unescape_cdata("".join(ch.itertext()))
            elif t == "link" and not link:
                link = (ch.get("href") or (ch.text or "")).strip()
            elif t in ("description", "summary", "content") and not desc:
                desc = _strip_html(_unescape_cdata("".join(ch.itertext())))
            elif t in ("pubdate", "published", "updated", "date") and not pub:
                pub = _unescape_cdata("".join(ch.itertext()))
        title = _strip_html(title)
        if not title or _is_junk(title):
            continue
        items.append(NewsItem(
            source_key=cfg.get("key", ""), source_name=cfg.get("name", ""),
            region=cfg.get("region", "GLOBAL"), lang=cfg.get("lang", "en"),
            kind=cfg.get("kind", "media"), title=title, url=link,
            summary=desc[:400], publish_time=_parse_time(pub),
            market=list(cfg.get("market") or []),
        ))
    return items


# ---------------------------------------------------------------- HTML 列表
def parse_html_list(html: str, cfg: dict) -> list[NewsItem]:
    """按 cfg['item_regex'] 从列表页抽条目（各站 HTML 结构不同，用正则钩子配置）。"""
    items: list[NewsItem] = []
    pattern = cfg.get("item_regex")
    if not pattern:
        return items
    base = cfg.get("base_url") or ""
    for m in re.finditer(pattern, html, re.I):
        gd = m.groupdict()
        raw = m.group(0)
        title = _strip_html(gd.get("title") or "")
        if len(title) < 4:                                    # 回退②：标签内 title="..." 属性
            mt = re.search(r'\btitle="([^"]{4,200})"', raw, re.I)
            if mt:
                title = _strip_html(mt.group(1))
        if len(title) < 4:                                    # 回退③：跳过紧邻标签后的文本
            mt = re.match(r"\s*(?:<[^>]{0,400}>\s*)*([^<]{4,200})", html[m.end(): m.end() + 600])
            if mt:
                title = _strip_html(mt.group(1))
        href = gd.get("url") or gd.get("href") or ""
        if len(title) < 4 or not href or _is_junk(title):
            continue
        if href and base and href.startswith("/"):
            href = base.rstrip("/") + href
        items.append(NewsItem(
            source_key=cfg.get("key", ""), source_name=cfg.get("name", ""),
            region=cfg.get("region", "CN"), lang=cfg.get("lang", "zh"),
            kind=cfg.get("kind", "media"), title=title, url=href,
            summary="", publish_time="", market=list(cfg.get("market") or []),
        ))
        if len(items) >= 60:
            break
    return items


# ---------------------------------------------------------------- 各类型抓取
def _fetch_rss(cfg: dict, timeout: int) -> list[NewsItem]:
    return parse_feed(http_get(cfg["url"], timeout=timeout), cfg)


def _fetch_html(cfg: dict, timeout: int) -> list[NewsItem]:
    html = http_get(cfg["url"], timeout=timeout).decode("utf-8", "ignore")
    return parse_html_list(html, cfg)


def _fetch_youtube(cfg: dict, timeout: int) -> list[NewsItem]:
    """YouTube Data API v3 检索（需 API Key）。"""
    key = os.environ.get(cfg.get("api_key_env", "YOUTUBE_API_KEY"), "") or cfg.get("api_key", "")
    if not key:
        raise RuntimeError("缺少 YouTube Data API Key（环境变量 YOUTUBE_API_KEY 或配置 api_key）")
    items: list[NewsItem] = []
    for q in (cfg.get("queries") or [])[:5]:
        qs = urllib.parse.urlencode({
            "part": "snippet", "q": q, "type": "video", "order": "relevance",
            "maxResults": int(cfg.get("max_results", 8)), "key": key})
        data = json.loads(http_get(f"https://www.googleapis.com/youtube/v3/search?{qs}",
                                   timeout=timeout).decode("utf-8", "ignore") or "{}")
        for it in data.get("items", []):
            sn = it.get("snippet", {}) or {}
            vid = (it.get("id", {}) or {}).get("videoId", "")
            items.append(NewsItem(
                source_key=cfg.get("key", ""), source_name=f"{cfg.get('name','YouTube')}·{sn.get('channelTitle','')}",
                region=cfg.get("region", "US"), lang=cfg.get("lang", "en"),
                kind="influencer", title=_strip_html(sn.get("title", "")),
                url=f"https://www.youtube.com/watch?v={vid}" if vid else "",
                summary=_strip_html(sn.get("description", ""))[:400],
                publish_time=_parse_time(sn.get("publishedAt", "")),
                market=list(cfg.get("market") or [])))
    return items


def _fetch_reddit(cfg: dict, timeout: int) -> list[NewsItem]:
    items: list[NewsItem] = []
    for sub in (cfg.get("subs") or [])[:4]:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit=15"
        data = json.loads(http_get(url, timeout=timeout).decode("utf-8", "ignore") or "{}")
        for ch in (data.get("data", {}) or {}).get("children", []):
            d = ch.get("data", {}) or {}
            items.append(NewsItem(
                source_key=cfg.get("key", ""), source_name=f"Reddit r/{sub}",
                region=cfg.get("region", "US"), lang="en", kind="forum",
                title=d.get("title", ""), url="https://www.reddit.com" + (d.get("permalink") or ""),
                summary=_strip_html(d.get("selftext", ""))[:300],
                publish_time=_parse_time(str(d.get("created_utc", ""))),
                market=list(cfg.get("market") or [])))
    return items


def _fetch_google_news(cfg: dict, timeout: int) -> list[NewsItem]:
    items: list[NewsItem] = []
    for q in (cfg.get("queries") or [])[:6]:
        url = ("https://news.google.com/rss/search?" +
               urllib.parse.urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}))
        items.extend(parse_feed(http_get(url, timeout=timeout), cfg))
    return items


def _fetch_xueqiu(cfg: dict, timeout: int) -> list[NewsItem]:
    raw = http_get(cfg["url"], timeout=timeout,
                   headers={"Referer": "https://xueqiu.com/", "Accept": "application/json"}).decode("utf-8", "ignore")
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    items: list[NewsItem] = []
    for it in (data.get("items") or data.get("list") or []):
        st = it.get("original_status") or it.get("status") or it
        title = _strip_html(st.get("title") or st.get("description") or "")[:120]
        if not title:
            continue
        items.append(NewsItem(
            source_key=cfg.get("key", ""), source_name=cfg.get("name", "雪球"),
            region="CN", lang="zh", kind=cfg.get("kind", "domestic_crosscheck"),
            title=title, url=f"https://xueqiu.com{st.get('target','')}" if st.get("target") else "",
            summary=_strip_html(st.get("description") or "")[:300],
            publish_time=_parse_time(str(st.get("created_at", ""))),
            market=list(cfg.get("market") or [])))
    return items


def _fetch_github_trending(cfg: dict, timeout: int) -> list[NewsItem]:
    """GitHub 官方 API 自算「趋势仓库」——近 N 天新建、按 star 增速排序。

    本机实测 api.github.com 可达（无需 VPN）；匿名限流 60 次/小时，
    配置 token_env（默认 GITHUB_TOKEN）后额度大幅提升。作为「技术热点」信号源。
    """
    import datetime
    days = int(cfg.get("days", 7))
    q = "created:>" + (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    extra = (cfg.get("query") or "").strip()
    if extra:
        q += " " + extra
    qs = urllib.parse.urlencode({
        "q": q, "sort": "stars", "order": "desc",
        "per_page": int(cfg.get("max_results", 15))})
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get(cfg.get("token_env", "GITHUB_TOKEN"), "") or cfg.get("token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.loads(http_get(f"https://api.github.com/search/repositories?{qs}",
                               timeout=timeout, headers=headers).decode("utf-8", "ignore") or "{}")
    items: list[NewsItem] = []
    for r in data.get("items", []) or []:
        full = r.get("full_name", "")
        stars = r.get("stargazers_count", 0)
        desc = _strip_html(r.get("description") or "")
        langs = r.get("language") or ""
        items.append(NewsItem(
            source_key=cfg.get("key", ""), source_name=cfg.get("name", "GitHub 趋势"),
            region=cfg.get("region", "GLOBAL"), lang="en", kind="tech_community",
            title=f"{full}（⭐{stars}）" + (f"：{desc}" if desc else ""),
            url=r.get("html_url", ""),
            summary=(f"语言 {langs} · 近 {days} 天新增 ⭐{stars} · " + desc)[:400],
            publish_time=_parse_time(r.get("created_at", "")),
            market=list(cfg.get("market") or [])))
    return items


def _pt_video_to_item(v: dict, cfg: dict, tag: str) -> NewsItem | None:
    name = _strip_html(v.get("name") or "")
    if not name:
        return None
    views = v.get("views")
    host = (v.get("account") or {}).get("host") or (v.get("channel") or {}).get("host") or ""
    url = v.get("url") or ""
    if url.startswith("/"):
        url = (cfg.get("instance") or "https://peertube.tv").rstrip("/") + url
    return NewsItem(
        source_key=cfg.get("key", ""), source_name=f"{cfg.get('name', 'PeerTube')}·{tag}",
        region=cfg.get("region", "GLOBAL"), lang="en", kind="video_platform",
        title=name, url=url,
        summary=(f"播放 {views if views is not None else '—'} · 实例 {host} · "
                 + _strip_html(v.get("description") or ""))[:400],
        publish_time=_parse_time(v.get("publishedAt", "")),
        market=list(cfg.get("market") or []))


def _fetch_peertube(cfg: dict, timeout: int) -> list[NewsItem]:
    """PeerTube（去中心化视频平台，境外「类 B 站」）：趋势榜 + 关键词搜索。

    peertube.tv 与 sepiasearch.org（全网索引）本机实测可达，无需 VPN。
    该平台内容以娱乐/个人创作为主，因此按 filter_keywords 做**相关性二次过滤**，
    只保留命中财经/时政/灾害等关键词的条目，避免娱乐视频污染舆情主题。
    """
    fk = [str(w).lower().strip() for w in (cfg.get("filter_keywords") or []) if str(w).strip()]
    # 用短语子串匹配（关键词均为多词短语，如 "stock market"），
    # 避免 "market" 命中 "Christmas Market"、"oil" 命中 "oil painting"
    kw_re = re.compile("|".join(re.escape(w) for w in fk), re.I) if fk else None

    def relevant(title: str) -> bool:
        return True if kw_re is None else bool(kw_re.search(title or ""))

    items: list[NewsItem] = []
    base = (cfg.get("instance") or "https://peertube.tv").rstrip("/")
    if cfg.get("include_trending", True):
        url = f"{base}/api/v1/videos?" + urllib.parse.urlencode({
            "sort": "-trending", "count": int(cfg.get("trending_count", 12)), "nsfw": "false"})
        try:
            data = json.loads(http_get(url, timeout=timeout).decode("utf-8", "ignore") or "{}")
        except Exception:  # noqa: BLE001
            data = {}
        for v in data.get("data", []) or []:
            it = _pt_video_to_item(v, cfg, "趋势")
            if it and relevant(it.title):
                items.append(it)
    search_host = (cfg.get("search_instance") or "https://sepiasearch.org").rstrip("/")
    for q in (cfg.get("queries") or [])[:4]:
        # 用实例自带搜索（真按关键词过滤）；全网索引的 query 过滤不可靠，仅作补充
        targets = [(base, {"search": q}), (search_host, {"searchTarget": "search-index", "query": q})]
        for host, params in targets:
            url = f"{host}/api/v1/search/videos?" + urllib.parse.urlencode(
                {**params, "count": 10, "nsfw": "false"})
            try:
                data = json.loads(http_get(url, timeout=timeout).decode("utf-8", "ignore") or "{}")
            except Exception:  # noqa: BLE001
                continue
            for v in data.get("data", []) or []:
                it = _pt_video_to_item(v, cfg, q)
                if it and relevant(it.title):
                    items.append(it)
    return items


def _fetch_relay(cfg: dict, timeout: int) -> list[NewsItem]:
    """读取「GitHub Actions 云中继」产出的 JSON（境外抓取 → 仓库 → jsDelivr/raw 回传）。

    被墙源（YouTube / Reuters / BBC / arXiv / GDELT / Reddit…）由 GitHub 的
    境外 runner 抓取并写成 JSON，本机只读取已生成好的文件，从而绕过访问限制。
    """
    raw = http_get(cfg["url"], timeout=timeout).decode("utf-8", "ignore")
    data = json.loads(raw or "{}")
    items: list[NewsItem] = []
    for it in data.get("items", []) or []:
        title = (it.get("title") or "").strip()
        if not title or _is_junk(title):
            continue
        items.append(NewsItem(
            source_key=cfg.get("key", ""),
            source_name=it.get("source_name") or cfg.get("name", "境外中继"),
            region=it.get("region") or cfg.get("region", "GLOBAL"),
            lang=it.get("lang", "en"),
            kind=it.get("kind", "media"),
            title=title, url=it.get("url", ""),
            summary=(it.get("summary") or "")[:400],
            publish_time=it.get("publish_time", ""),
            market=list(cfg.get("market") or [])))
    return items


_FETCHERS = {
    "rss": _fetch_rss,
    "html": _fetch_html,
    "youtube_api": _fetch_youtube,
    "reddit_json": _fetch_reddit,
    "google_news_rss": _fetch_google_news,
    "xueqiu_json": _fetch_xueqiu,
    "github_trending": _fetch_github_trending,
    "peertube": _fetch_peertube,
    "relay_json": _fetch_relay,
}


def fetch_source(cfg: dict, timeout: int = 12) -> SourceResult:
    """抓取单个源，永远返回 SourceResult（不抛异常），便于界面诚实展示状态。"""
    res = SourceResult(
        key=cfg.get("key", ""), name=cfg.get("name", ""), region=cfg.get("region", "GLOBAL"),
        enabled=bool(cfg.get("enabled")), requires_key=bool(cfg.get("requires_key")),
        requires_vpn=bool(cfg.get("requires_vpn")), note=cfg.get("note", ""),
    )
    if not cfg.get("enabled"):
        res.error = cfg.get("note") or "未启用"
        return res
    fn = _FETCHERS.get(cfg.get("type", "rss"))
    if fn is None:
        res.error = f"未知源类型 {cfg.get('type')}"
        return res
    t0 = time.time()
    try:
        res.items = fn(cfg, timeout)
        res.ok = True
    except Exception as e:  # noqa: BLE001
        res.ok = False
        res.error = f"{type(e).__name__}: {str(e)[:160]}"
    res.latency = time.time() - t0
    return res


def fetch_all(cfg_data: dict | None = None, timeout: int | None = None,
              only_enabled: bool = True, max_workers: int = 8) -> list[SourceResult]:
    """并发抓取全部（或仅启用的）源，返回顺序与配置一致。

    网络 IO 密集，8 路并发可把 30+ 源的抓取从 60~90 秒压到 10 秒级；
    返回顺序由 ex.map 保证与配置一致，便于界面稳定展示。
    """
    data = cfg_data or load_overseas_config()
    to = int(timeout or (data.get("defaults") or {}).get("timeout", 12))
    cfgs = sources_of(data, enabled_only=only_enabled)
    out: list[SourceResult] = []
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for r in ex.map(lambda c: fetch_source(c, timeout=to), cfgs):
            out.append(r)
    return out
