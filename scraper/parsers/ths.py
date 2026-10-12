"""同花顺股吧（t.10jqka.com.cn / 同花顺社区·论股圈子）解析器。

上线日期：2026-09-24
抓取对象：同花顺社区公开 feed（首页信息流）。该 feed 为服务端渲染的
``.feed-item`` 结构，公开可访问、无需登录。

域名实测结论（2026-09-24）：
- ``guba.10jqka.com.cn`` 根路径仅 69 字节的 JS 跳转脚本，跳到 ``forum.10jqka.com.cn``；
- ``forum.10jqka.com.cn`` 以及任意 ``/xxxx/`` 路径都会被 302 到 ``t.10jqka.com.cn``（同花顺社区）；
- 真正的股吧/圈子内容在 ``t.10jqka.com.cn``：首页 feed 含 40+ 条真实讨论帖子，
  个股页 ``/guba/<code>/`` 的帖子列表为 JS 动态加载，静态 HTML 里只有「热门股票」侧栏，
  故本解析器以**首页公开 feed** 作为 posts / news 的来源（含真实的作者、正文、链接）。
- 页面本身是 UTF-8（字节级核对过 UTF-8 中文序列确实存在于原始响应里），无需转码。

与 taoguba / eastmoney 解析器的合同一致：只解析 HTML，不发请求，不写库。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Iterable

from bs4 import BeautifulSoup

from ..bot_filter import is_bot_author
from ..models import NewsItem, Trade
from .base import BaseParser

logger = logging.getLogger(__name__)


# 中文动作词 -> 标准动作
ACTION_MAP = {
    "买入": "BUY", "建仓": "BUY", "加仓": "BUY", "全仓买入": "BUY",
    "卖出": "SELL", "清仓": "SELL", "减仓": "SELL", "止盈": "SELL",
    "止损": "SELL", "出货": "SELL", "全仓卖出": "SELL",
    "持有": "HOLD", "持仓": "HOLD", "持股": "HOLD",
}

# feed 帖子链接形如 http://t.10jqka.com.cn/circle/15109/  ->  15109
CIRCLE_RE = re.compile(r"/circle/(\d+)")
# 正文里「买入/卖出 + 6 位股票代码」的保守抽取
ACTION_RE = re.compile(r"(买入|建仓|加仓|全仓买入|卖出|减仓|清仓|持有|持仓|持股|止盈|止损|出货)")
CODE_RE = re.compile(r"(?<![0-9])([0-9]{6})(?![0-9])")

# 标准风险提示语（用于剔除纯免责声明条目，它们不是讨论）
RISK_HINT = "不作为买卖依据"


def parse_ths_time(text: str) -> datetime | None:
    """解析同花顺 feed 的时间文本。

    实测格式：``09-22 20:26``（MM-DD HH:MM，主流）、``2026-09-22 20:26:00``、
    ``今天 20:26`` / ``昨天 20:26``、``N分钟前`` / ``N小时前``、纯 ``HH:MM``。

    2026-09-25：本函数替代原先的 ``datetime.now()`` 兜底 —— 原来把**抓取时刻**
    当发帖时间写库，界面「时间」列显示的其实是抓取时间，会误导判断。
    解析不了返回 ``None``（宁可不显示，不冒充）。

    返回北京时间 naive（与库内 taoguba/xueqiu/sina 口径一致）。
    """
    now = datetime.now()
    t = (text or "").strip()
    if not t:
        return None
    m = re.search(r"(\d{1,3})\s*分钟前", t)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    m = re.search(r"(\d{1,2})\s*小时前", t)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    m = re.search(r"(\d+)\s*天前", t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    # 「今天/昨天/今日/昨日 HH:MM」
    m = re.search(r"(今天|今日|昨天|昨日)\s*(\d{1,2}):(\d{2})", t)
    if m:
        d = now.date() if m.group(1) in ("今天", "今日") else now.date() - timedelta(days=1)
        try:
            return datetime(d.year, d.month, d.day, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # 「2026-09-22 20:26[:00]」
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4) or 0), int(m.group(5) or 0))
        except ValueError:
            return None
    # 「09-22 20:26」（MM-DD HH:MM，无年份）：不晚于今天按今年，否则按去年
    m = re.search(r"(?<!\d)(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", t)
    if m:
        try:
            cand = datetime(now.year, int(m.group(1)), int(m.group(2)),
                            int(m.group(3)), int(m.group(4)))
        except ValueError:
            return None
        return cand if cand.date() <= now.date() else cand.replace(year=now.year - 1)
    # 纯「HH:MM」→ 今天
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        try:
            return datetime(now.year, now.month, now.day, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


class ThsParser(BaseParser):
    site_key = "ths"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base_url = (cfg.get("base_url") or "http://t.10jqka.com.cn").rstrip("/")
        self.symbol = (cfg.get("_key_meta") or cfg.get("symbol") or "")

    # ---------------- 反爬 / 登录墙守卫 ----------------
    def _is_blocked(self, html: str) -> bool:
        """同花顺公开 feed 一般不直接返回验证页，但做最小防御：
        - 页面过短（如 JS 跳转 stub / 空页）视为无内容；
        - 出现验证码 / 访问验证等字样则整页丢弃，避免假数据入库。"""
        if not html or len(html) < 800:
            return True
        return any(k in html for k in ("验证码", "访问验证", "操作太频繁", "请完成安全验证"))

    # ---------------- 公共抽取 ----------------
    def _items(self, html: str):
        soup = BeautifulSoup(html, "lxml")
        return soup.select(".feed-item")

    def _extract(self, it):
        """从一条 .feed-item 抽出 (post_url, text, author, time)。"""
        a = it.select_one(".feed-item-title a") or it.select_one(
            ".feed-item-broker a.broker-avatar"
        )
        url = a.get("href") if a else ""
        if url and not url.startswith("http"):
            url = self.base_url + url
        content = (
            it.select_one(".feed-item-content .word-content")
            or it.select_one(".feed-item-content .word-wrap")
            or it.select_one(".feed-item-content")
        )
        text = content.get_text(" ", strip=True) if content else ""
        author_el = it.select_one(".feed-item-broker .broker-name")
        author = author_el.get_text(strip=True) if author_el else ""
        time_el = it.select_one(".feed-item-timeline-time")
        t = time_el.get_text(strip=True) if time_el else ""
        return url, text, author, t

    # ---------------- 资讯 ----------------
    def parse_news(self, html: str, page_url: str) -> Iterable[NewsItem]:
        if self._is_blocked(html):
            return iter([])
        for it in self._items(html):
            url, text, author, t_txt = self._extract(it)
            if not text or len(text) < 10 or RISK_HINT in text:
                continue
            if not url or "/circle/" not in url:
                continue
            m = CIRCLE_RE.search(url)
            post_id = m.group(1) if m else url
            pt = parse_ths_time(t_txt)
            yield NewsItem(
                source_site=self.site_key,
                title=text[:60],
                url=url,
                # 解析不到就留空，绝不用抓取时刻冒充（2026-09-25 修复）
                publish_time=(pt.strftime("%Y-%m-%d %H:%M:%S") if pt else None),
                summary=text[:300],
                author=author or None,
            )

    # ---------------- 通用帖子（讨论）----------------
    def parse_posts(self, html: str, page_url: str) -> Iterable[dict]:
        if self._is_blocked(html):
            return iter([])
        for it in self._items(html):
            url, text, author, t_txt = self._extract(it)
            if not text or len(text) < 10 or RISK_HINT in text:
                continue
            if not url or "/circle/" not in url:
                continue
            m = CIRCLE_RE.search(url)
            post_id = m.group(1) if m else url
            pt = parse_ths_time(t_txt)
            yield {
                "author": author,
                "post_id": post_id,
                "post_url": url,
                "post_time": (pt.strftime("%Y-%m-%d %H:%M:%S") if pt else ""),
                "text": text,
            }

    # ---------------- 实盘交易（从帖子正文抽「动作 + 6 位代码」）----------------
    def parse_trades(self, html: str, page_url: str) -> Iterable[Trade]:
        if self._is_blocked(html):
            return iter([])
        seen: set[str] = set()
        for it in self._items(html):
            url, text, author, t_txt = self._extract(it)
            if is_bot_author(author)[0]:
                continue                      # 机器资讯号：其内容不进交易统计（2026-10-09 A）
            if not text or len(text) < 10:
                continue
            m_act = ACTION_RE.search(text)
            if not m_act:
                continue
            m_code = CODE_RE.search(text)
            symbol = m_code.group(1) if m_code else None
            if not symbol:
                continue
            action = ACTION_MAP.get(m_act.group(1), "UNKNOWN")
            if action == "UNKNOWN":
                continue
            m = CIRCLE_RE.search(url) if url else None
            post_id = m.group(1) if m else (url or text[:20])
            if post_id in seen:
                continue
            seen.add(post_id)
            pt = parse_ths_time(t_txt)
            yield Trade(
                source_site=self.site_key,
                author=author or "匿名",
                post_id=post_id,
                post_url=url or page_url,
                # 用页面时间节点解析；解析不到留空，绝不用抓取时刻冒充（2026-09-25 修复）
                post_time=(pt.strftime("%Y-%m-%d %H:%M:%S") if pt else None),
                action=action,
                symbol=symbol,
                symbol_name=None,
                price=None,
                quantity=None,
                raw_text=text[:400],
                access_level="free",
                access_hint="",
            )

    # ---------------- 选手榜 ----------------
    # 同花顺社区首页 feed 的作者是券商/个人账号，并非「实盘大赛」结构化排行榜；
    # 此处显式放弃（与 eastmoney 解析器一致），避免把论坛作者误标为实盘选手。
    def parse_players(self, html: str, page_url: str):
        return iter([])
