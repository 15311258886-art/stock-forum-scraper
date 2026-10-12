"""解析器接口。新增站点只需继承 BaseParser 并在 pipeline.PARSER_REGISTRY 注册。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from ..models import NewsItem, Trade


class BaseParser(ABC):
    site_key: str = ""

    @abstractmethod
    def parse_news(self, html: str, page_url: str) -> Iterable[NewsItem]:
        """从资讯列表页 HTML 解析出 NewsItem 列表。"""

    @abstractmethod
    def parse_trades(self, html: str, page_url: str) -> Iterable[Trade]:
        """从实盘/帖子列表页 HTML 解析出 Trade 列表。"""
