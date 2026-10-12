"""国内·近期热点：把「字面热词」聚合成「板块影响」卡片。

两个分离的数据源（与 hot_lexicon.py 的分工）：
- 事实层（本文件计算）：热词在 app.db 里的讨论帖数、参与人数、多空分布、涉及个股（名称从
  stock_names 补齐）—— 100% 来自真实数据，每个数字前端都可点击下钻。
- 判断层（hot_lexicon.py）：热词分别影响哪些板块、哪些上涨/承压、为什么 —— 投研逻辑，
  界面标注「投研判断」。未命中传导表的词不丢，归入「其他热词」。

输出按「能不能据此操作」分三层：
  层一 政策与行业主线（policy+industry，有明确板块指向）
  层二 交易节奏与资金情绪（rhythm，不指单一行业）
  层三 风险预警（risk，承压信号）
  + 其他热词（未命中传导表，仅作观察）

层内按「证据强度」降序：证据越足越靠前；样本少的如实标注，不伪装成强信号。
"""
from __future__ import annotations

from viewer import hot_lexicon as lex

LAYER_ORDER = ["main", "rhythm", "risk", "other"]
LAYER_META = {
    "main": {
        "title": "层一 · 政策与行业主线",
        "subtitle": "有明确板块指向 —— 直接给「上涨机会」与「承压回落」",
        "kinds": ("policy", "industry"),
    },
    "rhythm": {
        "title": "层二 · 交易节奏与资金情绪",
        "subtitle": "不指向单一行业 —— 回答「现在敢不敢动手、仓位怎么定」",
        "kinds": ("rhythm",),
    },
    "risk": {
        "title": "层三 · 风险预警",
        "subtitle": "承压信号 —— 回答「哪些地方要回避」",
        "kinds": ("risk",),
    },
    "other": {
        "title": "其他热词（暂无板块指向）",
        "subtitle": "未命中传导表，仅作观察，不影响判断",
        "kinds": (),
    },
}


def _name_of(db, code: str) -> str:
    row = db.conn.execute("SELECT name FROM stock_names WHERE code=?", (code,)).fetchone()
    return row["name"] if row else ""


import re as _re


def _match(text: str, term: str) -> bool:
    """判断帖文是否命中某个字面串。

    - 中文 / 长串：直接子串包含（中文无分词边界问题）。
    - 短英文（<=3 且全 ASCII，如 AI/GPT/CXO）：用单词边界，避免误命中
      MAIN / SAID / EMAIL 等内部子串。
    """
    if term.isascii() and len(term) <= 3:
        return bool(
            _re.search(
                r"(?<![A-Za-z0-9])" + _re.escape(term) + r"(?![A-Za-z0-9])",
                text,
                _re.IGNORECASE,
            )
        )
    return term in text


def _symbols_of(db, post_ids: set) -> list[dict]:
    if not post_ids:
        return []
    ph = ",".join("?" * len(post_ids))
    rows = db.conn.execute(
        f"SELECT DISTINCT symbol FROM stock_mentions WHERE post_id_ref IN ({ph})",
        list(post_ids),
    ).fetchall()
    seen = set()
    out = []
    for r in rows:
        code = r["symbol"]
        if not code or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": _name_of(db, code)})
    return out


def _evidence(db, terms: list[str], posts: list) -> dict:
    """扫描帖子列表，聚合命中 terms 中任一字面串的事实层证据。

    驱动源从「topics 表」改为「词库关键词直接扫原文」，因此即便 topics
    表是 n-gram 碎片、或为空，热点视图仍能基于真实讨论生成。
    """
    post_ids: set = set()
    authors: set = set()
    bull = bear = neutral = 0
    for p in posts:
        t = p["text"] or ""
        if not any(_match(t, term) for term in terms):
            continue
        post_ids.add(p["id"])
        if p["author"]:
            authors.add(p["author"])
        s = p["sentiment"]
        if s == "看多":
            bull += 1
        elif s == "看空":
            bear += 1
        else:
            neutral += 1
    # 证据强度（仅供排序，不展示给用户）：帖数*2 + 人数 + 多空失衡绝对值
    strength = len(post_ids) * 2 + len(authors) + abs(bull - bear)
    return {
        "posts": len(post_ids),
        "authors": len(authors),
        "bull": bull,
        "bear": bear,
        "neutral": neutral,
        "symbols": _symbols_of(db, post_ids),
        "strength": strength,
    }


def _item(topic: str, entry: dict | None, ev: dict) -> dict:
    if entry:
        return {
            "topic": topic,
            "kind": entry["kind"],
            "judgment": True,
            "meaning": entry.get("meaning", ""),
            "up": entry.get("up", []),
            "down": entry.get("down", []),
            "transmission": entry.get("transmission", ""),
            "decision": entry.get("decision", ""),
            "weight": ev["strength"],
            "evidence": ev,
        }
    # 未命中传导表：仅展示事实层，明确标注无板块判断
    return {
        "topic": topic,
        "kind": "unknown",
        "judgment": False,
        "meaning": "",
        "up": [],
        "down": [],
        "transmission": "",
        "decision": "",
        "weight": ev["strength"],
        "evidence": ev,
    }


def build_hotspots(db) -> dict:
    """聚合全部主线关键词，返回分层结构。db 为 viewer.db.Database 实例。

    驱动源改为「词库已知主线关键词」：直接扫帖子原文，不再依赖 topics 表
    （该表由 n-gram 抽取，中文下是碎片，几乎无法命中传导表）。只展示真有
    讨论的主线，空卡不出现；每个数字均可点击下钻到原始帖子。
    """
    posts = db.conn.execute(
        "SELECT id, author, sentiment, text FROM posts"
    ).fetchall()

    items: list[dict] = []
    for topic in lex.all_topics():
        terms = lex.match_terms(topic)
        ev = _evidence(db, terms, posts)
        if ev["posts"] == 0:
            continue  # 只展示真有讨论的主线，避免空卡
        entry = lex.entry_of(topic)
        items.append(_item(topic, entry, ev))

    layers: dict[str, list[dict]] = {k: [] for k in LAYER_ORDER}
    for it in items:
        kind = it["kind"]
        placed = False
        for lk in ("main", "rhythm", "risk"):
            if kind in LAYER_META[lk]["kinds"]:
                layers[lk].append(it)
                placed = True
                break
        if not placed:
            layers["other"].append(it)

    out_layers = []
    for lk in LAYER_ORDER:
        lst = layers[lk]
        if not lst:
            continue
        lst.sort(key=lambda x: x["evidence"]["strength"], reverse=True)
        out_layers.append({
            "key": lk,
            "title": LAYER_META[lk]["title"],
            "subtitle": LAYER_META[lk]["subtitle"],
            "items": lst,
        })

    return {
        "meta": {
            "title": "近期热点 · 从「热词」到「板块」",
            "role": "把社区里零散的字面热词，翻译成「哪些板块正被资金与情绪驱动」。",
            "value": "① 提前发现板块级机会 ② 避开情绪回落区 ③ 用真实讨论量校准「热」的真假 ④ 每个数字都能点开看原始依据。",
            "caliber": "事实层（帖数/人数/多空/涉及标的）实时来自 app.db，可点击下钻；判断层为投研传导分析，界面已标注「投研判断」，不构成投资建议。",
            "legend": "红 = 预判上涨 / 绿 = 预判承压（A 股惯例）。",
        },
        "layers": out_layers,
    }
