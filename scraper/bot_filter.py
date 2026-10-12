# -*- coding: utf-8 -*-
"""机器资讯号 / 播报号 识别（"非人轴"的**作者级**判定，2026-10-09 新增）。

背景（王总反馈）：选手池混入「平安银行资讯」这类**机器运营号**，其帖子被当作真人观点
参与「共识 / 共同持仓」统计，稀释真实信号。

与 quality_filter.py 里既有的「非人轴来源」名单的关系：
  · 既有名单是**站点级**（AXIS：eastmoney/ths/jisilu/moer = post），回答"这个来源整体回溯不到人"；
  · 本模块是**作者级**：同一站点内，逐昵称判断"这个号是人还是机器"。
  两者互补：站点级名单仍生效；作者级名单用于把**人站里的机器号**也摘出去。

判定维度（两段式，宁缺勿滥）：
  1) 昵称特征：平台/媒体品牌名 + 号类后缀（资讯/播报/快讯/日报/公告/官博/官微/客户端/服务号…）；
  2) 内容特征：整号语料里**没有任何第一人称买卖动词**，且**通稿/公告/转载句式**占比高。

对外接口：
  is_bot_author(name)                  -> (bool, reason)          # 仅昵称，供解析器热路径调用
  content_looks_like_press(texts)      -> (bool, reason)          # 仅内容，供离线复核
  classify_account(name, texts, site)  -> dict(is_bot, reason, evidence)
  BOT_NAMES                            -> 已确证名单（可被 quality_filter / tools 复用）

诚实原则：宁可漏判（放行可疑真人），不可误杀真人；无证据的疑点标 confidence=low。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 1) 已确证名单（人工逐条看过语料后确认，2026-10-09；可增删，王总可复核）
#    reason 里注明判据，便于回溯。
# ---------------------------------------------------------------------------
BOT_NAMES = {
    "神光财经":    "媒体号：异动点评/午间看盘通稿，无个人买卖",
    "Sina财经产":  "机器号：'弹幕欢乐炒股'刷屏，无正文/无个人观点",
    "新浪财经客":  "媒体号：【公告】/公司通稿搬运，无个人买卖",
    "新浪财经芝":  "媒体号：大盘/宏观通稿，无个人买卖",
    "看财经官博":  "媒体号：'发布了头条文章'/话题搬运，无个人买卖",
    "汽车营销分":  "数据通稿号：销量/换届等公司公告，无个人买卖",
    "机工战略":    "自媒体行业号：产业/宏观评论，无个人买卖",
    "EC品牌观察":  "自媒体行业号：消费品牌评论，无个人买卖",
    # 2026-10-10 补：前端工程师实测（证据已在 viewer/app.db 复核：posts 36 行、trades 2 行，
    # 均为 300750 宁德时代 的 HOLD）→ 确证机器号。昵称后缀正则未覆盖「…客户端」，故此前漏判。
    "新浪财经客户端": "客户端号：36 帖全为【公告】系、2 笔 HOLD（300750 宁德时代）系抄袭他人、零第一人称交易",
}

# ---------------------------------------------------------------------------
# 2) 昵称模式（保守，用于覆盖名单外的同类号）
# ---------------------------------------------------------------------------
_PLATFORM = (r"新浪|Sina|sina|腾讯|网易|搜狐|凤凰|和讯|财联社|证券之星|金融界|"
             r"同花顺|东方财富|格隆汇|新华|中金在线|每日经济|第一财经|界面|澎湃|"
             r"平安银行|招商银行|中金|雪球|证券时报|上海证券报|中国证券报")
_SUFFIX = (r"资讯|播报|快讯|日报|晚报|早报|午报|公告|情报|速递|时报|周刊|"
           r"看盘|解盘|盘点|官博|官微|情报站|客户端|服务号")
# 精确品牌名（单独出现即判媒体号）
_BRANDS = r"神光|益盟|大智慧|指南针|雪球财经|同花顺财经|东方财富网"

_NICKNAME_PATTERNS = [
    re.compile(rf"^(?:{_PLATFORM}).{{0,8}}(?:{_SUFFIX}|财经|观察|视点|焦点)$"),
    re.compile(rf"^.{{0,6}}(?:{_BRANDS}).{{0,6}}$"),
    re.compile(rf"^.{{0,8}}(?:{_SUFFIX})$"),
]

# ---------------------------------------------------------------------------
# 3) 内容特征
# ---------------------------------------------------------------------------
# 第一人称买卖动词（出现任一，视为"有个人交易"→ 偏向真人）
_TRADE_VERBS = ("买入", "买进", "卖出", "建仓", "清仓", "加仓", "减仓", "持仓", "持有",
                "低吸", "打板", "扫板", "排板", "割肉", "补仓", "上车", "下车", "出掉",
                "出货", "出局", "平仓", "止盈", "止损", "减持", "增持", "回补", "做T",
                "买了", "卖了", "持仓中", "建仓价", "成本价", "清了一半", "加了一点")
# 通稿 / 公告 / 转载 句式标记
_PRESS_MARKERS = ("公告称", "公告显示", "公告披露", "记者", "来源：", "来源:", "编辑：",
                  "转载", "免责声明", "发布了头条文章", "据报道", "报道称", "消息面",
                  "点评：", "快讯", "研报显示", "研报称", "业绩预告", "披露称",
                  "【", "】", "公司股票", "股份有限公司")

# 明显是刷屏/聚合的噪声标题
_SPAM_MARKERS = ("大家来吐槽吧", "弹幕欢乐炒股", "点击查看", "扫码", "开户链接", "广告")


def is_bot_author(name: str) -> tuple[bool, str]:
    """仅凭昵称判定（供解析器热路径调用）。命中确证名单或保守模式 → True。"""
    n = (name or "").strip()
    if not n:
        return False, ""
    if n in BOT_NAMES:
        return True, f"确证名单：{BOT_NAMES[n]}"
    for rx in _NICKNAME_PATTERNS:
        if rx.search(n):
            return True, f"昵称命中规律：{rx.pattern}"
    return False, ""


def content_looks_like_press(texts: list[str]) -> tuple[bool, str]:
    """整号语料：无第一人称买卖动词 且 通稿/公告/刷屏标记显著 → 疑似机器号。"""
    joined = " ".join(t or "" for t in texts)
    if not joined.strip():
        return False, "无文本"
    has_trade = any(v in joined for v in _TRADE_VERBS)
    press = sum(joined.count(m) for m in _PRESS_MARKERS)
    spam = sum(joined.count(m) for m in _SPAM_MARKERS)
    n_posts = max(1, len(texts))
    press_ratio = press / n_posts
    if spam >= 1:
        return True, f"含刷屏/聚合标记（{spam} 次），疑似机器号"
    if not has_trade and press >= 3 and press_ratio >= 1.0:
        return True, f"无第一人称买卖 + 通稿/公告句式密集（{press} 次 / {n_posts} 帖）"
    return False, ""


def classify_account(name: str, texts: list[str], site: str = "") -> dict:
    """综合判定：返回 {is_bot, reason, evidence, confidence, site}。"""
    by_name, why_name = is_bot_author(name)
    by_text, why_text = content_looks_like_press(texts)
    if by_name and by_text:
        return {"is_bot": True, "reason": f"{why_name}；且{why_text}",
                "confidence": "high", "site": site, "evidence": why_name}
    if by_name:
        # 昵称像机器号，但内容有个人交易 → 降级为"疑似"，不误杀
        return {"is_bot": not any(v in " ".join(texts) for v in _TRADE_VERBS),
                "reason": f"{why_name}（内容未见个人交易）" if not any(
                    v in " ".join(texts) for v in _TRADE_VERBS) else f"{why_name}，但内容含个人交易",
                "confidence": "medium" if by_text or not any(
                    v in " ".join(texts) for v in _TRADE_VERBS) else "low",
                "site": site, "evidence": why_name}
    if by_text:
        return {"is_bot": True, "reason": why_text, "confidence": "medium",
                "site": site, "evidence": why_text}
    return {"is_bot": False, "reason": "未见机器号特征", "confidence": "low",
            "site": site, "evidence": ""}
