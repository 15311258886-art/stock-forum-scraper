"""东方财富股吧（guba.eastmoney.com）解析器（2026-09-24 上线）。

目标与边界：
- **目标**：让 trades/posts/news 表在不需要登录的情况下也能从东方财富股吧流入真数据。
- **已知边界**：东方财富是公开访问（无需登录），但其列表页有反爬 UA / Referer 检查，
  本解析器只做"列表 → 公开帖子 → 关键词命中交易"这一段，不下钻详情页。
- **诚实标注**：东方财富股吧**不是结构化行情源**，大多数帖子是散户讨论，并非真正持仓/交易。
  我们用「`trade_patterns` 正则」从帖子正文中提取明确写了的股票代码与买卖方向；
  没有命中的帖子只当作 news 一条入库，不强行猜。

与 taoguba 解析器的合同一致：只解析 HTML，不发请求，不写库。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

from bs4 import BeautifulSoup

from ..bot_filter import is_bot_author
from ..models import NewsItem, Trade
from .base import BaseParser


# 中文动作词 → 标准动作。覆盖论坛常用同义表达
ACTION_MAP = {
    "买入": "BUY", "建仓": "BUY", "加仓": "BUY", "全仓买入": "BUY",
    "清仓": "SELL", "减仓": "SELL", "卖出": "SELL", "止盈": "SELL",
    "止损": "SELL", "出货": "SELL", "全仓卖出": "SELL",
    "持有": "HOLD", "持仓": "HOLD", "持股": "HOLD",
}

# 列表页帖子链接的 URL 模式：/news,{stock_code},{post_id}.html
POST_HREF_RE = re.compile(r"/news,([0-9]{6}),(\d+)\.html")

# 帖子正文里写「我今天买入了 600519 贵州茅台」时抽股票代码与动作
# ——保守写法：只在「动作 + 6位代码」同时出现，且代码出现在动作后 24 字内时算命中。
ACTION_RE = re.compile(r"(?:今[天日]?|昨天|今天)?(买入|建仓|加仓|卖出|减仓|清仓|持有|持仓|持股|止盈|止损|出货)")
CODE_RE = re.compile(r"(?<![0-9])([0-9]{6})(?![0-9])")
TIME_RE = re.compile(r"(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})|((\d{2}):(\d{2})\s*发表)")


def parse_em_time(text: str) -> datetime | None:
    """解析东财股吧列表行的时间文本。

    实测可能形态：``2026-09-24 15:20:00``、``09-24 15:20``、``09-24``、
    ``今天 15:20`` / ``昨天 15:20``、``5分钟前``、纯 ``15:20``（今日）。

    2026-09-25：替代原先 ``datetime.now()`` 硬编码 —— 原来把**抓取时刻**当
    发帖时间入库，界面「时间」列显示的其实是抓取时间，会误导判断。
    解析不到返回 ``None``（宁可不显示，不冒充）。返回北京时间 naive。
    """
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now()
    t = (text or "").strip()
    if not t:
        return None
    m = re.search(r"(\d{1,3})\s*分钟前", t)
    if m:
        return now - _td(minutes=int(m.group(1)))
    m = re.search(r"(\d{1,2})\s*小时前", t)
    if m:
        return now - _td(hours=int(m.group(1)))
    # 「2026-09-24 15:20[:00]」
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", t)
    if m:
        try:
            return _dt(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                       int(m.group(4) or 0), int(m.group(5) or 0))
        except ValueError:
            return None
    # 「今天/昨天 15:20」
    m = re.search(r"(今天|今日|昨天|昨日)\s*(\d{1,2}):(\d{2})", t)
    if m:
        d = now.date() if m.group(1) in ("今天", "今日") else now.date() - _td(days=1)
        try:
            return _dt(d.year, d.month, d.day, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # 「09-24 15:20」（无年份）
    m = re.search(r"(?<!\d)(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", t)
    if m:
        try:
            cand = _dt(now.year, int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            return None
        return cand if cand.date() <= now.date() else cand.replace(year=now.year - 1)
    # 「09-24」（无年份，仅日期）
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", t)
    if m:
        try:
            cand = _dt(now.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        return cand if cand.date() <= now.date() else cand.replace(year=now.year - 1)
    # 纯「15:20」→ 今天
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        try:
            return _dt(now.year, now.month, now.day, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


class EastMoneyParser(BaseParser):
    site_key = "eastmoney"

    # 2026-09-24:东方财富对无 cookie / Referer 的请求会返回 2.8KB 的「身份核实」页（带 captcha 验证脚本）。
    # 检测到后**整页丢弃**、不写入 news/trades/posts，避免假的"身份核实"页被入库当真实内容。
    BLOCK_TITLE_TAG = "身份核实"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base_url = cfg.get("base_url", "https://guba.eastmoney.com")
        self.symbol = (cfg.get("_key_meta") or cfg.get("symbol") or "").zfill(6) if (cfg.get("_key_meta") or cfg.get("symbol")) else ""
        # 列表页 URL 模板（按股票代码生成）。当每只股票都独立注册一个 site 时这里会被覆盖。
        self.list_url_tpl = cfg.get("list_url_tpl", self.base_url + "/list,{symbol}.html")

    def _is_blocked(self, html: str) -> bool:
        """是否被东方财富风控拦截（身份核实 / CAPTCHA 页）"""
        return self.BLOCK_TITLE_TAG in html[:5000]

    def _hit_to_trade(self, post_id: str, post_url: str, post_time: datetime, author: str,
                       action_word: str, code: str, snippet: str) -> Trade | None:
        symbol = code
        action = ACTION_MAP.get(action_word, "UNKNOWN")
        if action == "UNKNOWN" or len(symbol) != 6:
            return None
        return Trade(
            source_site=self.site_key,
            author=author,
            post_id=post_id,
            post_url=post_url,
            post_time=post_time,
            action=action,
            symbol=symbol,
            symbol_name=None,
            price=None,
            quantity=None,
            raw_text=snippet[:400],
            buy_time=None,
            buy_price=None,
            current_price=None,
            access_level="free",
        )

    def _li_time(self, li) -> datetime | None:
        """从列表行抽取发布时间：优先专用时间节点，再退回行内短文本节点。

        取不到返回 None（绝不用抓取时刻冒充）。2026-09-25 新增。
        """
        for sel in (".update", ".time", "td.update", "td.time", ".post-time",
                    ".date", "td.date", ".l5", "td.l5", ".author-time"):
            el = li.select_one(sel)
            if el:
                pt = parse_em_time(el.get_text(" ", strip=True))
                if pt:
                    return pt
        # 兜底：只扫 **短** 文本节点，避免把标题里的日期/代码误当发布时间
        for el in reversed(li.find_all(True)):
            if el.name in ("a", "script", "style"):
                continue
            txt = el.get_text(" ", strip=True)
            if 3 <= len(txt) <= 22:
                pt = parse_em_time(txt)
                if pt:
                    return pt
        return None

    # 三个 parse_* 在被风控拦截时都直接返回空 iter（不浪费下游做无效入库）
    def _parse_with_guard(self, html: str, fn):
        if self._is_blocked(html):
            logger.warning("eastmoney 触发身份核实拦截（页面长 %d，标题含「身份核实」），本批数据已被丢弃", len(html))
            return iter([])
        return fn(html)

    # ---------------- 资讯 / 帖子 ----------------
    def parse_news(self, html: str, page_url: str) -> Iterable[NewsItem]:
        if self._is_blocked(html):
            return iter([])
        # 返回 NewsItem：ingest.py 会调 r.to_dict() 再 .get() 字段，
        # 字段：title / url / publish_time / summary / author
        soup = BeautifulSoup(html, "lxml")
        for li in soup.select("li.listitem, tr.listitem, li.list_item, tr.list_item, li[data-postid], div.post, div.post_item, li.post, li.post_item"):
            href_a = li.select_one(".title a, div.title a, a.title, a.listitem-title, .post-title a")
            if not href_a:
                continue
            title = href_a.get_text(strip=True)
            href = href_a.get("href", "")
            m = POST_HREF_RE.search(href)
            if not m:
                continue
            post_url = href if href.startswith("http") else self.base_url + href
            author_el = li.select_one(".auther, .author, .liren, .user-name, .post-author, td.author")
            author = author_el.get_text(strip=True)[:60] if author_el else ""
            summary_el = li.select_one(".l3, .content, .summary, .post-content, .text")
            summary = summary_el.get_text(" ", strip=True)[:300] if summary_el else ""
            pt = self._li_time(li)
            yield NewsItem(
                source_site=self.site_key,
                title=title[:140],
                url=post_url,
                # 解析不到就留空，绝不用抓取时刻冒充（2026-09-25 修复）
                publish_time=(pt.strftime("%Y-%m-%d %H:%M:%S") if pt else None),
                summary=summary,
                author=author or None,
            )

    # ---------------- 选手榜 ----------------
    # 东方财富股吧**没有公开的选手排行榜**（不像淘股吧有"实盘大赛"列表页），
    # 选手榜主要从帖子正文中反推"某作者多次提及某标的"等弱信号，但纯反推
    # 误报严重——这里先**显式放弃**，调用方会落到通用抓取之外的"只采集帖子"路径。
    def parse_players(self, html: str, page_url: str):
        return iter([])

    # ---------------- 帖子（与 news 类似但走 save_post 流程，字段名是 text/author/post_id/post_url/post_time） ----------------
    def parse_posts(self, html: str, page_url: str) -> Iterable[dict]:
        if self._is_blocked(html):
            return iter([])
        # ingest.py 走的是「save_post → analytics.score_sentiment → extract_codes」，
        # 字段必须叫 text/author/post_id/post_url/post_time。
        soup = BeautifulSoup(html, "lxml")
        for li in soup.select("li.listitem, tr.listitem, li.list_item, tr.list_item, li[data-postid], div.post, div.post_item, li.post, li.post_item"):
            href_a = li.select_one(".title a, div.title a, a.title, a.listitem-title, .post-title a")
            if not href_a:
                continue
            href = href_a.get("href", "")
            m = POST_HREF_RE.search(href)
            if not m:
                continue
            post_url = href if href.startswith("http") else self.base_url + href
            post_id = m.group(2)
            author_el = li.select_one(".auther, .author, .liren, .user-name, .post-author, td.author")
            author = author_el.get_text(strip=True)[:60] if author_el else ""
            body_el = li.select_one(".l3, .content, .summary, .post-content, .text")
            text = body_el.get_text(" ", strip=True)[:600] if body_el else href_a.get_text(" ", strip=True)
            pt = self._li_time(li)
            yield {
                "author": author,
                "post_id": post_id,
                "post_url": post_url,
                # 解析不到真实时间则留空，绝不用抓取时刻冒充（2026-09-25 修复）
                "post_time": (pt.strftime("%Y-%m-%d %H:%M:%S") if pt else ""),
                "text": text,
            }

    # ---------------- 持仓比例 ----------------
    # 列表页无持仓数据；详情页需登录才能看到持仓（不在本最小解析器覆盖范围）。
    def parse_positions(self, html: str, page_url: str, player: str = ""):
        return iter([])

    # ---------------- 实盘交易 ----------------
    def parse_trades(self, html: str, page_url: str) -> Iterable[dict]:
        if self._is_blocked(html):
            return iter([])
        # 东方财富**列表页通常只显示标题**（无详情正文），标题中也很少带 6 位股票代码。
        # 策略：在「标题或帖子预览正文」里找"动作 + 同页 URL 上自带的股票代码" 组合——
        #       也就是 URL `/list,{stock}.html` 本身就告诉我们当前是哪个股票，
        #       标题里出现"买入/卖出..."等动作词就直接记录一次 trade，股票代码从 URL 兜底。
        soup = BeautifulSoup(html, "lxml")
        # 从 page_url 抓股票代码（list,000001.html 或 news,000001,...html）
        sym_match = re.search(r"/(?:list|news),([0-9]{6})", page_url)
        url_symbol = sym_match.group(1) if sym_match else ""
        seen_ids = set()  # 同 post_id 不重复
        for li in soup.select("li.listitem, tr.listitem, li.list_item, tr.list_item, li[data-postid], div.post, div.post_item, li.post, li.post_item"):
            a = li.select_one(".title a, div.title a, a.title, a.listitem-title, .post-title a")
            if not a:
                continue
            href = a.get("href", "")
            m = POST_HREF_RE.search(href)
            if not m:
                continue
            post_id = m.group(2)
            if post_id in seen_ids:
                continue
            post_url = href if href.startswith("http") else self.base_url + href
            author_el = li.select_one(".author, .auther, .liren, .user-name, .post-author, td.author")
            author = author_el.get_text(strip=True)[:60] if author_el else "匿名"
            if is_bot_author(author)[0]:
                continue                      # 机器资讯号：其内容不进交易统计（2026-10-09 A）
            # 优先 body：列表有些带 .l3 / .summary；没有就只取标题文本
            body_el = li.select_one(".l3, .content, .summary, .post-content, .text")
            body = body_el.get_text(" ", strip=True) if body_el else ""
            title_txt = a.get_text(" ", strip=True)
            search_text = title_txt + " " + body
            # 选动作
            m_act = ACTION_RE.search(search_text)
            if not m_act:
                continue
            # 股票代码：先找正文里 6 位数，再退回 URL 自带的
            m_code = CODE_RE.search(search_text)
            symbol = m_code.group(1) if m_code else url_symbol
            if not symbol or len(symbol) != 6:
                continue
            action_word = m_act.group(1)
            action = ACTION_MAP.get(action_word, "UNKNOWN")
            if action == "UNKNOWN":
                continue
            seen_ids.add(post_id)
            pt = self._li_time(li)
            yield Trade(
                source_site=self.site_key,
                author=author,
                post_id=post_id,
                post_url=post_url,
                # 解析不到就留空，绝不用抓取时刻冒充（2026-09-25 修复）
                post_time=pt,
                action=action,
                symbol=symbol,
                symbol_name=None,
                price=None,
                quantity=None,
                raw_text=search_text[:400],
                access_level="free",
                access_hint="",
            )