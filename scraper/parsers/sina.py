"""新浪股市汇（guba.sina.com.cn）解析器（2026-09-25 上线）。

目标与边界：
- **目标**：新浪财经股吧（现名"股市汇"）的公开讨论与博主人气榜接入，与东财/淘股吧同构。
- **页面事实**（2026-09-25 实测，匿名可访问、无需登录）：
  * 个股社区页：`/?s=bar&name=sh600519`（沪 sh / 深 sz 前缀 + 6 位代码），
    服务端渲染的帖子列表为 <tr> 行：阅读数 / 回复数 / 标题链接 / 作者 / 时间。
  * 帖子详情：`/?s=thread&tid={tid}&bid={bid}`（参数顺序不固定，两个都要）。
  * 首页含「作者人气榜」表（排名 em.number_em_* / 作者 a.js_show_hoverCard / 人气值），
    是现成的博主打榜数据，直接作为 players 来源。
- **编码坑**：页面为 GBK（gb2312），HTTP 头不声明 charset，requests 会按 ISO-8859-1
  错解——本解析器不做解码（parser 合同：只解析传入的 HTML 字符串）；
  编码修正由 ingest/pipeline 在访问 resp.text **之前**完成（resp.encoding="gb18030"）。
- **诚实标注**：列表页只有标题没有正文，posts 的 text 用标题兜底（与东财同策略）；
  交易识别同样走「标题动作词 + URL 股票代码」保守组合，不强行猜。

与 taoguba/eastmoney 解析器的合同一致：只解析 HTML，不发请求，不写库。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

from bs4 import BeautifulSoup

from ..bot_filter import is_bot_author
from ..models import NewsItem, Trade
from .base import BaseParser


# 中文动作词 → 标准动作（与东财保持一致口径）
ACTION_MAP = {
    "买入": "BUY", "建仓": "BUY", "加仓": "BUY", "全仓买入": "BUY",
    "清仓": "SELL", "减仓": "SELL", "卖出": "SELL", "止盈": "SELL",
    "止损": "SELL", "出货": "SELL", "全仓卖出": "SELL",
    "持有": "HOLD", "持仓": "HOLD", "持股": "HOLD",
}

ACTION_RE = re.compile(r"(买入|建仓|加仓|卖出|减仓|清仓|持有|持仓|持股|止盈|止损|出货)")
TID_RE = re.compile(r"[?&]tid=(\d+)")
BID_RE = re.compile(r"[?&]bid=(\d+)")
UID_RE = re.compile(r"/u/(\d+)")
# 社区页 URL 里的股票代码：name=sh600519 / name=sz000001（裸 6 位也兼容）
SYMBOL_RE = re.compile(r"[?&]name=(?:sh|sz)?(\d{6})")

# 被删帖/受限页守卫（2026-09-25 实测：?s=thread 缺 tid 时会返回「本帖已删除,或访问受限」壳页）
_BLOCK_MARKS = ("本帖已删除,或访问受限", "本帖已删除，或访问受限", "出错了")


def _parse_rel_time(text: str) -> datetime | None:
    """解析新浪列表页的相对时间。

    实测格式：「今天08:22」「昨天20:31」「09月24日」「2026-09-20」「HH:MM」「N分钟前」。

    2026-09-25 修复（重要）：原实现解析不了时 `return now`，即把**抓取时刻**
    当成帖子发布时间写库，导致界面「时间」列显示的是抓取时间而非真实发帖时间，
    属于会误导判断的错误数据。现改为解析不了返回 None，由调用方跳过时间字段
    （前端显示「—」），宁可不显示，也不冒充。
    另补上「MM月DD日」格式（页面中超过 1 天的帖子即用此格式，原先未覆盖 → 全部
    回退成抓取时刻，是本 bug 的主因）。

    返回北京时间（naive；调用方按需加 +08:00 / 转 UTC）。
    """
    now = datetime.now()
    t = (text or "").strip()
    if not t:
        return None
    m = re.search(r"(\d{1,3})分钟前", t)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    m = re.search(r"(\d{1,2})小时前", t)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    # 「今天08:22」「昨天20:31」（兼容「今日」「昨日」「昨」）
    dm = re.search(r"(今天|今日|昨天|昨日|昨)\s*(\d{1,2}):(\d{2})", t)
    if dm:
        d = now.date() if dm.group(1) in ("今天", "今日") else now.date() - timedelta(days=1)
        try:
            return datetime(d.year, d.month, d.day, int(dm.group(2)), int(dm.group(3)))
        except ValueError:
            return None
    # 「09月24日」（无年份：不晚于今天按今年，晚于今天按去年，避免未来日期）
    m = re.search(r"(\d{1,2})月(\d{1,2})日", t)
    if m:
        try:
            cand = datetime(now.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        if cand.date() > now.date():
            try:
                cand = cand.replace(year=now.year - 1)
            except ValueError:
                return None
        return cand
    # 「2026-09-20」「2026/09/20」
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # 「09-20」（无年份，两位数月日）
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", t)
    if m:
        try:
            cand = datetime(now.year, int(m.group(1)), int(m.group(2)))
            return cand if cand.date() <= now.date() else cand.replace(year=now.year - 1)
        except ValueError:
            return None
    # 「HH:MM」→ 视为今天
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        try:
            return datetime(now.year, now.month, now.day, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


class SinaParser(BaseParser):
    site_key = "sina"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base_url = cfg.get("base_url", "https://guba.sina.com.cn")

    @staticmethod
    def _is_blocked(html: str) -> bool:
        head = html[:6000]
        return any(mark in head for mark in _BLOCK_MARKS)

    def _abs(self, href: str) -> str:
        return href if href.startswith("http") else self.base_url + href

    # ---------------- 帖子行通用抽取（个股社区页 <tr> 行） ----------------
    def _bar_rows(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        rows: list[dict] = []
        seen: set[str] = set()
        for a in soup.select("a[href*=tid]"):
            href = a.get("href", "")
            mt = TID_RE.search(href)
            if not mt:
                continue
            tid = mt.group(1)
            if tid in seen:
                continue
            tr = a.find_parent("tr")
            if tr is None:
                continue  # 精选帖等 div 容器不算列表行（首页 b_cont 区块）
            author = ""
            author_url = ""
            a_el = tr.select_one("div.author a, .author a")
            if a_el:
                author = (a_el.get("title") or a_el.get_text(strip=True) or "")[:60]
                author_url = self._abs(a_el.get("href", ""))
            tds = tr.find_all("td")
            time_text = tds[-1].get_text(" ", strip=True) if tds else ""
            rows.append({
                "tid": tid,
                "title": a.get_text(" ", strip=True),
                "href": self._abs(href),
                "author": author,
                "author_url": author_url,
                "time": _parse_rel_time(time_text),
                # 源页是否给出了具体时刻（HH:MM）。只有日期（如「09月24日」）时为 False，
                # 此时绝不补「00:00」这种假钟点，只保留到日（2026-09-25 修复）。
                "time_precise": bool(re.search(r"\d{1,2}:\d{2}", time_text)),
            })
            seen.add(tid)
        return rows

    @staticmethod
    def _url_symbol(page_url: str) -> str:
        m = SYMBOL_RE.search(page_url or "")
        return m.group(1) if m else ""

    # ---------------- 资讯 ----------------
    def parse_news(self, html: str, page_url: str) -> Iterable[NewsItem]:
        if self._is_blocked(html):
            return iter([])
        tz_cn = timezone(timedelta(hours=8))
        for r in self._bar_rows(html):
            yield NewsItem(
                source_site=self.site_key,
                title=(r["title"] or "")[:140],
                url=r["href"],
                # 时间为 None 时留空，不用抓取时刻冒充（2026-09-25 修复）；
                # 源页只有日期时存「YYYY-MM-DD」字符串，不补零点的假钟点。
                # 口径统一：全库时间均为**北京时间 naive**（与 taoguba/xueqiu 一致），
                # 故此处不再转 UTC，否则界面会比真实时间早 8 小时。
                publish_time=((r["time"].strftime("%Y-%m-%d") if not r["time_precise"]
                               else r["time"].strftime("%Y-%m-%d %H:%M:%S"))
                              if r["time"] else None),
                summary="",
                author=r["author"] or None,
            )

    # ---------------- 帖子 ----------------
    def parse_posts(self, html: str, page_url: str) -> Iterable[dict]:
        if self._is_blocked(html):
            return iter([])
        for r in self._bar_rows(html):
            yield {
                "author": r["author"] or "匿名",
                "post_id": r["tid"],
                "post_url": r["href"],
                "post_time": ((r["time"].strftime("%Y-%m-%d") if not r["time_precise"]
                               else r["time"].strftime("%Y-%m-%d %H:%M:%S"))
                              if r["time"] else ""),
                "text": (r["title"] or "")[:600],  # 列表页无正文，标题兜底（与东财同策略）
            }

    # ---------------- 选手榜（首页「作者人气榜」表） ----------------
    def parse_players(self, html: str, page_url: str) -> list[dict]:
        if self._is_blocked(html):
            return []
        soup = BeautifulSoup(html, "lxml")
        out: list[dict] = []
        seen: set[str] = set()

        def add(name, source_url, rank=None, score=None):
            name = (name or "").strip()
            if not name or name in seen:
                return
            seen.add(name)
            out.append({
                "name": name,
                "rank_position": int(rank) if rank else None,
                "ranking_score": score,
                "source_url": source_url,
                "is_certified": 0,
                "tags": [],
            })

        for a in soup.select('a.js_show_hoverCard[href*="/u/"]'):
            tr = a.find_parent("tr")
            if tr is None:
                continue  # 精选帖头像不算榜单
            rank_em = tr.select_one("em.number_em_01, em.number_em_02")
            rank = None
            if rank_em:
                rm = re.search(r"\d+", rank_em.get_text(strip=True))
                rank = int(rm.group(0)) if rm else None
            # 人气值：作者列之后第一个 color_gray 数字单元格
            score = None
            gray = tr.select_one("td.color_gray")
            if gray:
                sm = re.search(r"(\d+)", gray.get_text(strip=True).replace(",", ""))
                score = int(sm.group(1)) if sm else None
            m = UID_RE.search(a.get("href", ""))
            url = f"{self.base_url}/u/{m.group(1)}" if m else (page_url or self.base_url)
            add(a.get("title") or a.get_text(strip=True), url, rank, score)
        return out

    # ---------------- 持仓比例 ----------------
    # 列表页无持仓数据；新浪股市汇无公开持仓页。显式放弃（与东财一致）。
    def parse_positions(self, html: str, page_url: str, player: str = ""):
        return iter([])

    # ---------------- 实盘交易（标题动作词 + URL 股票代码，保守组合） ----------------
    def parse_trades(self, html: str, page_url: str) -> Iterable[Trade]:
        if self._is_blocked(html):
            return iter([])
        url_symbol = self._url_symbol(page_url)
        for r in self._bar_rows(html):
            if is_bot_author(r["author"])[0]:
                continue                      # 机器资讯号：其内容不进交易统计（2026-10-09 A）
            text = r["title"] or ""
            m_act = ACTION_RE.search(text)
            if not m_act:
                continue
            action = ACTION_MAP.get(m_act.group(1))
            if action not in ("BUY", "SELL", "HOLD"):
                continue
            symbol = url_symbol
            if not symbol or len(symbol) != 6:
                continue
            yield Trade(
                source_site=self.site_key,
                author=r["author"] or "匿名",
                post_id=r["tid"],
                post_url=r["href"],
                # 有时刻用 datetime，仅日期用「YYYY-MM-DD」字符串（ingest._ts 两种都兼容）
                post_time=((r["time"].strftime("%Y-%m-%d") if not r["time_precise"] else r["time"])
                           if r["time"] else None),
                action=action,
                symbol=symbol,
                symbol_name=None,
                price=None,
                quantity=None,
                raw_text=text[:400],
                access_level="free",
                access_hint="",
            )
