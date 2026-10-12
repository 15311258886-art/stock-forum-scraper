"""国际局势 · 全球事件驱动 A 股影响研判。

子模块：
  taxonomy  事件分类体系（六类）+ 关键词自动分类 + 兜底影响映射
  store     geo_events 表建表 / 幂等导入 / 查询 + 近期国际新闻筛选

数据来源分两层（诚实口径）：
  · 研判事件（seed_events.json，人工+AI 联网调研）—— 自带方向判断与代表个股；
  · 新闻流（overseas_news 表内筛出的国际类条目）—— 只做分类与展示，**不给方向**。
"""
from __future__ import annotations

from pathlib import Path

GEO_DIR = Path(__file__).resolve().parent
SEED_PATH = GEO_DIR / "seed_events.json"

__all__ = ["GEO_DIR", "SEED_PATH"]
