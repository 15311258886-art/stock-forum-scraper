# -*- coding: utf-8 -*-
"""选手个人主页解析（新增能力，纯解析，不发请求、不写库）。

【为什么需要它】
采集器原有 targets 面向「站点列表页」（按股票代码/话题抓），不支持「按选手个人主页逐个
采集」。而选手名单（227 人）给的都是**个人主页 URL**，落到三种不同页面上，且其中两种
现有解析器抓不动：

  · 淘股吧 /blog/<id>   博客列表页：DOM 为 div.allblog_article > div.article_tittle，
                        只列「标题 + 发表时间」，**无正文**；正文在逐篇 /a/<id> 详情页的
                        div.article-text。→ 需两步：先列表后详情。
                        现有 TaogubaParser 依赖 p.topic-info（那是 /talk/talkSeq 话题页的
                        结构），在博客页命中 0，故抓不动。
  · 新浪股吧 /u/<uid>  用户中心「主贴」列表：DOM 为 div.item_list（div 布局，**不是 <tr>**）。
                        现有 SinaParser._bar_rows 要求 a.find_parent("tr")，故命中 0。
  · 雪球 /u/<uid>      与搜索页同构（article.timeline__item），现有 XueqiuParser 直接可用。

本模块只补前两种形态的解析；交易的语义抽取（词典驱动的股票名识别、量价抽取）不重写，
而是复用既有解析器内核：
  · 淘股吧详情页正文 → TaogubaParser._extract_trades（见 tools/collect_players.py）
  · 雪球主页        → XueqiuParser.parse_posts / parse_trades

与既有解析器同一合同：只解析 HTML 字符串，不发请求、不写库。
"""
from __future__ import annotations

import re
from typing import Iterable

from bs4 import BeautifulSoup

# 淘股吧帖子 id：/a/<id>
_TAOGUBA_POST_RE = re.compile(r"/a/([A-Za-z0-9]+)")
# 新浪帖子 tid：?s=thread&bid=<bid>&tid=<tid>
_SINA_TID_RE = re.compile(r"[?&]tid=(\d+)")
_SINA_BID_RE = re.compile(r"[?&]bid=(\d+)")
# 「2026-09-17」形态的日期
_DATE_YYYYMMDD_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _abs(base: str, href: str) -> str:
    """把相对链接补成绝对 URL。"""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return base.rstrip("/") + "/" + href


# --------------------------------------------------------------------------- #
# 淘股吧：博客列表页 /blog/<id>
# --------------------------------------------------------------------------- #
def parse_taoguba_blog(html: str, base_url: str, selectors: dict | None = None) -> list[dict]:
    """解析淘股吧选手博客列表页，返回该选手的帖子条目（标题 + 时间，无正文）。

    返回每项：{post_id, post_url, title, time}。命中不到列表结构时返回 []。
    """
    selectors = selectors or {}
    # div.article_tittle 是列表行；它有时包在 div.allblog_article 里、有时直接裸露
    # （2026-10-09 实测：部分选手博客无 allblog_article 包裹层）。故直接选 article_tittle，
    # 两种形态都能命中；表头用的是另一个类 div.article_top，不会误选。
    sel_list = selectors.get("blog_list", "div.article_tittle")
    sel_title = selectors.get("blog_title", ".tittle_data a[href*='/a/']")
    sel_time = selectors.get("blog_time", ".tittle_fbshijian")

    soup = BeautifulSoup(html or "", "lxml")
    out: list[dict] = []
    seen: set[str] = set()
    for row in soup.select(sel_list):
        a = row.select_one(sel_title)
        if not a:
            continue
        href = a.get("href", "")
        m = _TAOGUBA_POST_RE.search(href)
        post_id = m.group(1) if m else ""
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)
        tm_el = row.select_one(sel_time)
        raw_time = tm_el.get_text(" ", strip=True) if tm_el else ""
        dm = _DATE_YYYYMMDD_RE.search(raw_time)
        out.append({
            "post_id": post_id,
            "post_url": _abs(base_url, href),
            "title": (a.get("title") or a.get_text(strip=True) or "").strip(),
            # 博客列表只给到「日」，不补假的钟点（与 sina 解析器同一诚实口径）
            "time": f"{dm.group(0)}" if dm else raw_time[:20],
        })
    return out


def parse_taoguba_article(html: str, selectors: dict | None = None) -> dict:
    """解析淘股吧帖子详情页 /a/<id>，返回 {title, body}。

    正文容器 div.article-text；抽不到时返回 {}（调用方据此跳过，绝不硬凑）。
    """
    selectors = selectors or {}
    sel_body = selectors.get("article_body", "div.article-text")
    sel_title = selectors.get("article_title", "div.article-tittle")

    soup = BeautifulSoup(html or "", "lxml")
    body_el = soup.select_one(sel_body)
    if body_el is None:
        return {}
    body = body_el.get_text(" ", strip=True)
    if not body:
        return {}
    title_el = soup.select_one(sel_title)
    title = title_el.get_text(strip=True) if title_el else ""
    return {"title": title, "body": body}


def taoguba_article_time(html: str) -> str:
    """从淘股吧详情页尽量取发布时间（失败返回空串，不冒充抓取时刻）。"""
    soup = BeautifulSoup(html or "", "lxml")
    el = soup.select_one("div.article-data")
    if el is None:
        el = soup.select_one("span.article-data-time, .article-time")
    if el is None:
        return ""
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", el.get_text(" ", strip=True))
    return f"{m.group(1)} {m.group(2)}" if m else ""


# --------------------------------------------------------------------------- #
# 新浪股吧：用户中心「主贴」列表 /u/<uid>
# --------------------------------------------------------------------------- #
def parse_sina_user(html: str, base_url: str, selectors: dict | None = None) -> list[dict]:
    """解析新浪股吧选手主页 /u/<uid> 的「主贴」列表（div 结构，非 <tr>）。

    返回每项：{author, post_id, post_url, post_time, title, text}。
    text 取 ilt_p 摘要（列表页无正文，摘要兜底，与既有东财/sina 列表页同策略）；
    时间用 SinaParser._parse_rel_time 解析，解析不了留空（不冒充抓取时刻）。
    """
    from .parsers.sina import _parse_rel_time  # 复用其相对时间解析口径（模块级函数）

    selectors = selectors or {}
    sel_item = selectors.get("item", "div.item_list")
    sel_author = selectors.get("author", ".ilt_name a")
    sel_title = selectors.get("title", "h4.ilt_tit a[href*=tid]")
    sel_text = selectors.get("text", "p.ilt_p")

    soup = BeautifulSoup(html or "", "lxml")
    out: list[dict] = []
    seen: set[str] = set()
    for item in soup.select(sel_item):
        a_title = item.select_one(sel_title)
        if not a_title:
            continue
        href = a_title.get("href", "")
        m = _SINA_TID_RE.search(href) or _SINA_TID_RE.search(str(item.get("tid", "")))
        tid = m.group(1) if m else ""
        if not tid or tid in seen:
            continue
        seen.add(tid)
        a_author = item.select_one(sel_author)
        author = ""
        if a_author:
            author = (a_author.get_text(strip=True) or "")[:60]
        # 时间：面板内首个链接（形如「10月08日」/「今天08:22」）
        tm_el = item.select_one(".ilt_panel .fl_left span a") or item.select_one(".ilt_panel a")
        raw_time = tm_el.get_text(" ", strip=True) if tm_el else ""
        ptime = _parse_rel_time(raw_time)
        text_el = item.select_one(sel_text)
        block_id = str(item.get("bid", "")) or ""
        bid_m = _SINA_BID_RE.search(href)
        if not block_id and bid_m:
            block_id = bid_m.group(1)
        out.append({
            "author": author,
            "post_id": tid,
            "post_url": _abs(base_url, href),
            "post_time": (ptime.strftime("%Y-%m-%d")
                          if ptime and not re.search(r"\d{1,2}:\d{2}", raw_time)
                          else (ptime.strftime("%Y-%m-%d %H:%M:%S") if ptime else "")),
            "title": (a_title.get("title") or a_title.get_text(strip=True) or "").strip(),
            "text": (text_el.get_text(" ", strip=True) if text_el else "")[:600],
            "bar_id": block_id,
        })
    return out


# --------------------------------------------------------------------------- #
# 统一入口（供 collect_players 调用）
# --------------------------------------------------------------------------- #
def parse_home(site_key: str, html: str, page_url: str, cfg: dict) -> Iterable[dict]:
    """按站点把个人主页 HTML 解析成统一帖子 dict。

    统一字段：author / post_id / post_url / post_time / title / text。
    （淘股吧博客列表无正文，text 用标题兜底；正文抽取由调用方按需再抓详情页。）
    """
    if site_key == "sina":
        for p in parse_sina_user(html, cfg.get("base_url", ""), cfg.get("home_selectors")):
            yield p
    elif site_key == "taoguba":
        for p in parse_taoguba_blog(html, cfg.get("base_url", ""), cfg.get("home_selectors")):
            yield {
                "author": "",
                "post_id": p["post_id"],
                "post_url": p["post_url"],
                "post_time": p["time"],
                "title": p["title"],
                "text": p["title"],
            }
    else:  # 其它站点（如雪球）由调用方直接用其原生解析器
        return
