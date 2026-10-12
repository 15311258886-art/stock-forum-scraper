"""数据模型：只承载“公开内容”层面的字段，不含任何用户行为 / 画像数据。

字段说明：
- author：选手在帖子中主动署名的昵称 / ID。这是“内容作者”属性，不是隐私抓取，
  因为该信息由用户在公开发帖时自行披露。合规层不会把它用于用户画像关联。
- 所有“在线/活跃/互动/粉丝/注册时间/地域/设备”等字段一律不出现在模型中。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Union


class _Base:
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trade(_Base):
    """一条被解析出的实盘交易记录。"""
    source_site: str
    author: str                 # 选手公开发帖署名
    post_id: str
    post_url: str
    post_time: Optional[Union[datetime, str]]  # 发布时间；None=源未给（不冒充抓取时刻）；str=仅日期「YYYY-MM-DD」（源未给时刻）
    action: str                 # BUY / SELL / HOLD / UNKNOWN
    symbol: Optional[str] = None        # 股票代码（6 位）
    symbol_name: Optional[str] = None   # 股票名称
    price: Optional[float] = None       # 成交价（如帖中披露）
    quantity: Optional[int] = None      # 股数（如帖中披露）
    raw_text: str = ""                  # 命中段落原文（便于人工复核）
    # —— 实盘完整明细（详情页核心字段）——
    buy_time: Optional[datetime] = None     # 买入/成交时间（优先于 post_time）
    buy_price: Optional[float] = None       # 买入价（成交价）
    current_price: Optional[float] = None   # 当前股价（行情源，可能为空）
    access_level: str = "free"             # free / paid / points / locked
    access_hint: str = ""                  # 受限原因提示（如“需开通会员”）
    extracted_at: datetime = field(default_factory=datetime.now)


@dataclass
class NewsItem(_Base):
    """一条一手市场消息 / 资讯。"""
    source_site: str
    title: str
    url: str
    publish_time: Optional[Union[datetime, str]] = None  # str=仅日期（源未给时刻）
    summary: str = ""
    author: Optional[str] = None        # 资讯来源署名（公开）
