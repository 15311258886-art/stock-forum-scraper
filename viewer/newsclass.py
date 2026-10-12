# -*- coding: utf-8 -*-
"""新闻 / 公告分类规则 —— **唯一真源**（2026-10-01 口径自查收编）。

改前存在两份独立副本，同一条标题前后端会给出不同结果：

  · server.py 的 ``_ANN_TITLE_RULES`` —— 公告研判（个股公告流 → 立场）
  · app.js    的 ``newsClassify rules`` —— 个股新闻卡片打分类标签

两者词表高度重叠但各自维护（人事治理少了「会计师事务所」、风险监管的 ``ST``
写法有误等），属于典型的「同一概念两把尺」，与 2026-10-01「筹码集中度」同型。
现统一到本模块：

  · 后端：server.py 调 ``classify_ann()``
  · 前端：GET /api/news_rules 取 ``rules_payload()``，脚本启动时写一份
    ``static/news_rules.js``（``window.NEWS_RULES``），前端同步读取

口径自述：按规则表**顺序**取首个命中（先看业绩方向 → 业绩披露 → 股东行为
→ 风险监管 → 融资/人事/重组），全不命中归「其他事务 / neutral」。
改规则只改本文件，前后端同步生效。
"""
from __future__ import annotations

import json
import os
import re

ANN_TITLE_RULES: list[tuple[str, str, str]] = [
    # (类别, 倾向 good/bad/neutral, 标题正则) —— 顺序即优先级
    ("业绩向好", "good", r"预增|预盈|扭亏|增长|上升|提升|创新高|超预期"),
    ("业绩承压", "bad", r"预减|预亏|下滑|下降|亏损|低于预期"),
    ("业绩披露", "neutral", r"业绩(?:预告|快报|报表?|说明会)|年度报告|半年度报告|季度报告|季报|年报|中报|摘要"),
    ("股东回报", "good", r"权益分派|分红|派息|现金红利|利润分配|回购|回报|增持"),
    ("股东减持", "bad", r"减持"),
    ("股权激励", "good", r"股权激励|员工持股"),
    ("风险监管", "bad", r"立案|处罚|监管函|问询函?|警示|诉讼|仲裁|违规|冻结|退市风险|\*?ST"),
    ("融资发债", "neutral", r"发行|募集|发债|可转债|配股|定增|向特定对象"),
    ("人事治理", "neutral", r"辞职|聘任|聘请|选举|换届|股东大会|董事会|监事会|职工代表|决议|章程|会计师事务所|法律意见书|风险评估报告|会计政策|变更|更正"),
    ("重组并购", "good", r"重组|并购|收购|重大资产"),
]
_COMPILED = [(c, t, re.compile(p)) for c, t, p in ANN_TITLE_RULES]


def classify(title: str) -> tuple[str, str]:
    """标题 → (类别, 倾向)。按规则表顺序首个命中。"""
    t = title or ""
    for cat, tone, rx in _COMPILED:
        if rx.search(t):
            return cat, tone
    return ("其他事务", "neutral")


def rules_payload() -> list[dict]:
    """给前端的规则表（cat / tone / pattern 三元组，前端按同样顺序匹配）。"""
    return [{"cat": c, "tone": t, "pattern": p} for c, t, p in ANN_TITLE_RULES]


def write_js(path: str) -> bool:
    """把规则表落成 static/news_rules.js（window.NEWS_RULES），前端同步读取。

    由 server.py 启动时调用一次；规则改了重启即生效，避免前后端各维护一份。
    """
    try:
        body = "/* 自动生成，唯一真源 = viewer/newsclass.py，请勿手改 */\n" \
               "window.NEWS_RULES = " + json.dumps(rules_payload(), ensure_ascii=False) + ";\n"
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        return False
