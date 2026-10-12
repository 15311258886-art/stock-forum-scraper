"""外部参考 · 第三方判断信号的汇聚与回测（**独立隔离模块**）。

隔离设计（硬约束，2026-09-27 依用户要求设立）：
  · 目录独立——`extref/` 与 `geo/` 平级，不 import viewer 内部业务模块；
  · 表独立——只用 `extref_sources` / `extref_signals` 两张表，前缀隔离；
  · 零耦合——**不 JOIN、不外键、不回写** posts / trades / players / geo_events / lhb_stocks
    等任何既有表；既有表的数据将来若被引流进来，只走"只读派生"（另立 sid，可追溯）；
  · API 独立——命名空间 `/api/extref_*`，与既有接口不共用返回结构；
  · 前端独立——一级导航「外部参考」，不复用「实盘选手」「行情研判」的渲染结构。

为什么要隔离：本模块存的是**别人（软件/榜单/社区/机构）的判断**，与项目自有抓取数据
（论坛帖子、实盘流水、龙虎榜）在**可信度口径上完全不同**，混在一张表或一个面板里会相互
稀释——用户明确要求"不要让数据之间相互冲突地混搭"。

子模块：
  taxonomy  信源种类/分档/取数通道/信号硬度 的定义与中文标签
  store     extref_sources / extref_signals 建表 + 幂等导入 + 查询 + 胜率聚合

数据来源分两层（诚实口径）：
  · 信源档案（seed_sources.json）——S1 名单，含是否持牌、取数通道、接入状态；
  · 信号样本（seed_signals.json）——首批真实可核的公开信号，带原始口径与可点外链。
"""
from __future__ import annotations

from pathlib import Path

EXTRref_DIR = Path(__file__).resolve().parent
SEED_SOURCES = EXTRref_DIR / "seed_sources.json"
SEED_SIGNALS = EXTRref_DIR / "seed_signals.json"

__all__ = ["EXTRref_DIR", "SEED_SOURCES", "SEED_SIGNALS"]
