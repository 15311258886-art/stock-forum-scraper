"""海外舆情与热点模块（模块一：美股港股舆情监控；模块二：近期热点/国外新闻驱动）。

子模块
------
- sources : 多类型数据源适配（RSS / HTML / YouTube API / Reddit / Google News / 雪球）
- lexicon : 中英情绪词库、标的识别、主题归类、海外主题→国内板块传导表
- collect : 采集编排（抓取 → 分析 → 落库 → 情绪时间序列）
- hotspots: 热点聚合与「提升性影响分析」
- seed    : 结构化示例条目（被墙源的补位，界面明确标注「示例」）
"""
from __future__ import annotations

__all__ = ["sources", "lexicon", "collect", "hotspots", "seed"]
