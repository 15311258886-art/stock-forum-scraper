"""雪球（xueqiu.com）解析器（2026-09-25 上线，浏览器渲染 HTML 路线）。

背景与诚实边界（已实测）：
- 雪球反爬为「阿里云 WAF（JS 挑战）+ 滑块验证码」双保险。裸 requests 打 JSON API
  （statuses/search.json 等）会被拦：要么 400016 需登录，要么返回 WAF 挑战页，要么空 200。
- 但**匿名**用真实浏览器（puppeteer-core / Chromium）可过 WAF、拿到匿名 xq_a_token，
  且 **讨论「搜索结果页」`https://xueqiu.com/k?q=<代码>` 不会弹滑块**，能渲染出真实帖子。
  （个股时间线页 /S/SHxxxx 会弹滑块，故我们走搜索页。）
- 因此本解析器解析**搜索结果页的 HTML**（BeautifulSoup），配合 ingest 侧用
  `scraper/browser_fetch.py` 的浏览器 fetch 拿到渲染后 HTML。解析对象为
  `article.timeline__item`：作者(a.user-name) / 时间来源(a.date-and-source) /
  正文(div.content.content--description) / 标题(h3.timeline__item__title) /
  互动数(a.timeline__item__control)。status_id 从 data-analytics-data 的 JSON 取，
  user_id 从头像 a 的 href 取，拼成帖子 URL。
- `_is_blocked()`：识别 WAF 挑战页 / 滑块验证页（aliyun_waf / 拖动到最右边 / 访问验证），
  命中即丢弃本批、不报错（浏览器若被再次挑战，落库 0 行，属预期）。
- 不把行情端点伪装成讨论，不写 demo 数据。

与 taoguba / eastmoney 解析器合同一致：只解析（HTML），不自行发请求、不写库。
实际发请求由 ingest 调用（xueqiu 走 browser_fetch）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ..bot_filter import is_bot_author
from .. import name_match as nm
from ..models import NewsItem, Trade
from .base import BaseParser

logger = logging.getLogger(__name__)

# 风控/拦截特征
WAF_MARKERS = ("aliyun_waf", "renderData", "_waf_", "拖动到最右边", "请按住滑块", "访问验证")
TIME_RE = re.compile(r"(\d{2}-\d{2}\s+\d{2}:\d{2})")
# 个人主页/时间线常给「相对时间」：今天 20:50 / 昨天 09:12 / 前天 15:00 / 12分钟前 / 3小时前 / 刚刚。
# （2026-10-09 补：原 _norm_time 只认「MM-DD HH:MM」，导致个人主页近 2 天的帖子发布时间全空。）
_TIME_TODAY_RE = re.compile(r"(今天|昨天|前天|刚刚)\s*(\d{1,2}):(\d{2})")
_TIME_AGO_RE = re.compile(r"(\d{1,3})\s*(分钟前|小时前)")
_AUTHOR_YEAR = 2026  # 雪球讨论时间缺少年份，按当前年份补全（2026-09 上下文）


class XueqiuParser(BaseParser):
    site_key = "xueqiu"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base_url = cfg.get("base_url", "https://xueqiu.com")

    # ---------------- 反爬守卫 ----------------
    def _is_blocked(self, html: str) -> bool:
        if not html or len(html) < 50:
            return True
        if any(m in html[:6000] for m in WAF_MARKERS):
            return True
        # 浏览器若被弹滑块，正文里会出现「访问验证 / 拖动滑块」字样
        if "访问验证" in html[:6000] or "请按住滑块" in html[:6000]:
            return True
        return False

    # ---------------- 工具 ----------------
    @staticmethod
    def _norm_time(s: str) -> Optional[str]:
        """把雪球时间串规范成「YYYY-MM-DD HH:MM:SS」（解析不了返回 None，不冒充）。

        支持两种形态（2026-10-09 扩充相对时间）：
          ① 相对时间（个人主页/时间线）：刚刚 / 今天 20:50 / 昨天 09:12 / 前天 15:00 / 12分钟前 / 3小时前；
          ② 绝对时间（较旧帖子）：MM-DD HH:MM（缺年份按 _AUTHOR_YEAR 补全）。
        口径与既有实现一致：产出**北京本地 naive** 时间字符串。
        """
        s = s or ""
        now = datetime.now()
        if "刚刚" in s:
            return now.strftime("%Y-%m-%d %H:%M:%S")
        m = _TIME_AGO_RE.search(s)
        if m:
            n = int(m.group(1))
            dt = now - timedelta(minutes=n) if m.group(2) == "分钟前" else now - timedelta(hours=n)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        m = _TIME_TODAY_RE.search(s)
        if m:
            tag, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
            if tag == "刚刚":
                dt = now
            else:
                days = {"今天": 0, "昨天": 1, "前天": 2}[tag]
                base = now - timedelta(days=days)
                try:
                    dt = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
                except ValueError:
                    return None
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        m = TIME_RE.search(s)
        if not m:
            return None
        try:
            dt = datetime.strptime(f"{_AUTHOR_YEAR}-{m.group(1)}", "%Y-%m-%d %H:%M")
            return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @staticmethod
    def _status_and_user(item) -> tuple[str, str]:
        """从 article 里取 (status_id, user_id)。"""
        status_id = ""
        user_id = ""
        # status_id 藏在 data-analytics-data 的 JSON 里
        for el in item.select("[data-analytics-data]"):
            raw = el.get("data-analytics-data", "")
            if "status_id" in raw:
                try:
                    d = json.loads(raw.replace("&quot;", '"'))
                    status_id = str(d.get("status_id", ""))
                    if status_id:
                        break
                except (json.JSONDecodeError, ValueError):
                    m = re.search(r"status_id&quot;?:(\d+)", raw)
                    if m:
                        status_id = m.group(1)
                        break
        # user_id：头像 a 的 href 形如 /7913104177
        avatar = item.select_one("a.avatar, a.avatar-40")
        if avatar:
            href = avatar.get("href", "")
            m = re.search(r"/(\d{4,})", href)
            if m:
                user_id = m.group(1)
        return status_id, user_id

    @staticmethod
    def _post_url(status_id: str, user_id: str) -> str:
        if status_id and user_id:
            return f"https://xueqiu.com/{user_id}/{status_id}"
        if status_id:
            return f"https://xueqiu.com/{status_id}"
        return "https://xueqiu.com"

    # ---------------- 通用帖子（讨论，主路径）----------------
    def parse_posts(self, html: str, page_url: str) -> Iterable[dict]:
        if self._is_blocked(html):
            return iter([])
        soup = BeautifulSoup(html, "lxml")
        for art in soup.select("article.timeline__item"):
            info = art.select_one(".timeline__item__info, a.date-and-source")
            author_el = art.select_one("a.user-name")
            author = author_el.get_text(strip=True) if author_el else (
                (info.get_text(" ", strip=True).split("修改于")[-1].split("·")[0].strip()
                 if info else "unknown")
            )
            if not author:
                author = "unknown"
            body = art.select_one("div.content.content--description") or art.select_one(
                ".timeline__item__bd"
            )
            text = body.get_text(" ", strip=True) if body else art.get_text(" ", strip=True)
            if not text:
                continue
            status_id, user_id = self._status_and_user(art)
            time_raw = info.get_text(" ", strip=True) if info else ""
            post_time = self._norm_time(time_raw) or ""
            yield {
                "author": author,
                "post_id": status_id or (user_id + "_" + text[:12]),
                "post_url": self._post_url(status_id, user_id),
                "post_time": post_time,
                "text": text,
            }

    # ---------------- 资讯（同一讨论流，派生为 news）----------------
    def parse_news(self, html: str, page_url: str) -> Iterable[NewsItem]:
        if self._is_blocked(html):
            return iter([])
        soup = BeautifulSoup(html, "lxml")
        for art in soup.select("article.timeline__item"):
            info = art.select_one(".timeline__item__info, a.date-and-source")
            author_el = art.select_one("a.user-name")
            author = author_el.get_text(strip=True) if author_el else None
            body = art.select_one("div.content.content--description") or art.select_one(
                ".timeline__item__bd"
            )
            text = body.get_text(" ", strip=True) if body else art.get_text(" ", strip=True)
            if not text:
                continue
            title_el = art.select_one("h3.timeline__item__title")
            title = (title_el.get_text(strip=True) if title_el else text[:60]).strip()
            status_id, user_id = self._status_and_user(art)
            time_raw = info.get_text(" ", strip=True) if info else ""
            ptime = None
            nt = self._norm_time(time_raw)
            if nt:
                try:
                    ptime = datetime.strptime(nt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    ptime = None
            yield NewsItem(
                source_site=self.site_key,
                title=title,
                url=self._post_url(status_id, user_id),
                publish_time=ptime,
                summary=text[:200],
                author=author,
            )

    # ---------------- 实盘交易 ----------------
    # 雪球用 $股票名(SH/SZ代码)$ 标记化引用；仅当同一帖内出现明确动作词（买入/卖出/持有…）
    # 才记为交易，避免把纯讨论当交易。代码取自标记里的 SH/SZ 6 位代码。
    _XQ_STOCK_RE = re.compile(r"\((?:SH|SZ|sh|sz)(\d{6})\)")
    _XQ_ACTION_RE = re.compile(
        r"(买入|建仓|加仓|全仓买入|买进|低吸|卖出|减仓|清仓|平仓|止盈|止损|割肉|持仓|持有)")
    _XQ_ACTION_MAP = {
        "买入": "BUY", "建仓": "BUY", "加仓": "BUY", "全仓买入": "BUY",
        "买进": "BUY", "低吸": "BUY", "卖出": "SELL", "减仓": "SELL",
        "清仓": "SELL", "平仓": "SELL", "止盈": "SELL", "止损": "SELL",
        "割肉": "SELL", "持仓": "HOLD", "持有": "HOLD",
    }

    # 动作词与 $名(代码)$ 标注之间的字符距离上限（治"一句多票"，2026-10-09）。
    # 旧实现：取帖内**第一个**动作词，套用到帖内**所有**代码上（1 动作 → N 票），
    # 「谈持仓，$宁德(SZ300750)$$茅台(SH600519)$都不错」会被判成同时买入两只。
    # 现改为：每个代码取**最近**的动作词，且距离 ≤ 本上限；无邻近买卖动词 → 仅是提及，不入交易。
    _XQ_ASSOC_GAP = 20

    def _xq_action_for(self, text: str, s: int, e: int) -> str:
        """给一个代码标注区间 [s,e)，找最近的 1 个动作词（≤ _XQ_ASSOC_GAP 字）；无则返回 ''。"""
        best = None
        best_gap = self._XQ_ASSOC_GAP + 1
        for m in self._XQ_ACTION_RE.finditer(text):
            apos, aend = m.start(), m.end()
            if apos >= e:
                gap = apos - e
            elif aend <= s:
                gap = s - aend
            else:
                gap = 0
            if gap >= best_gap:
                continue
            best, best_gap = m.group(1), gap
        return self._XQ_ACTION_MAP.get(best, "") if best else ""

    def parse_trades(self, html: str, page_url: str) -> Iterable[Trade]:
        if self._is_blocked(html):
            return iter([])
        soup = BeautifulSoup(html, "lxml")
        for art in soup.select("article.timeline__item"):
            author_el = art.select_one("a.user-name")
            author = author_el.get_text(strip=True) if author_el else "匿名"
            if is_bot_author(author)[0]:
                continue                      # 机器资讯号：其内容不进交易统计（2026-10-09 A）
            body = art.select_one("div.content.content--description") or art.select_one(
                ".timeline__item__bd")
            text = body.get_text(" ", strip=True) if body else art.get_text(" ", strip=True)
            if not text:
                continue
            for m in self._XQ_STOCK_RE.finditer(text):
                code = m.group(1)
                action = self._xq_action_for(text, m.start(), m.end())
                if action not in ("BUY", "SELL", "HOLD"):
                    continue                      # 无邻近买卖动词 → 提及，不入交易
                # P2（2026-10-10）：补上 symbol_name（此前恒为 None）。code_name 已排除指数名，
                #   优先返回同码的个股规范名（000001→平安银行）。
                sname = nm.code_name(code)
                if sname and nm.is_index_name(sname):
                    sname = None                      # 兜底：绝不把指数名当个股名
                status_id, user_id = self._status_and_user(art)
                info = art.select_one(".timeline__item__info, a.date-and-source")
                time_raw = info.get_text(" ", strip=True) if info else ""
                post_time = self._norm_time(time_raw) or ""
                yield Trade(
                    source_site=self.site_key,
                    author=author,
                    post_id=status_id or (user_id + "_" + text[:12]),
                    post_url=self._post_url(status_id, user_id),
                    post_time=post_time,
                    action=action,
                    symbol=code,
                    symbol_name=sname,
                    price=None,
                    quantity=None,
                    raw_text=text[:400],
                    access_level="free",
                    access_hint="",
                )
