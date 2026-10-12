"""海外舆情采集编排：抓取 → 分析（情绪/主题/标的/板块）→ 落库 → 生成情绪时间序列。

对外主入口：`collect(db, ...)`，返回本次采集摘要（含每个源的状态，便于界面诚实展示）。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta

from overseas import lexicon as lx
from overseas.sources import (
    NewsItem, SourceResult, fetch_all, load_overseas_config, sources_of,
)


# 来源先验兜底：关键词无法归类时，按信源性质归入最贴近的主题。
# 理由：科研刊物/交易所公告的专业术语无法穷举，用来源性质兜底比丢进「其他」更有信息量。
_KIND_TOPIC_FALLBACK = {
    "research": "科研论文与突破",      # Nature / Phys.org / Live Science …
    "official": "金融与汇率",          # HKEX 公告、监管披露
    "tech_community": "科技与AI",      # Hacker News / GitHub 趋势
}


def analyze_item(item: NewsItem, topics_cfg: dict, region_weights: dict) -> dict:
    """把一条原始新闻分析为可入库的结构化记录。"""
    blob = f"{item.title} {item.summary}"
    sentiment, score = lx.score_sentiment(blob)
    topic, topics = lx.classify_topic(blob, topics_cfg)
    if topic == "其他":                       # 来源先验兜底，减少无信息量的「其他」桶
        fb = _KIND_TOPIC_FALLBACK.get(item.kind or "")
        if fb:
            topic, topics = fb, [fb]
    entities = lx.extract_symbols(blob)
    # 关联国内板块：优先用传导表命中，其次用标的反查
    sectors: list[str] = []
    for row in lx.match_transmission(blob):
        sectors.extend(row["sectors"])
    for e in entities:
        if e["market"] == "CN":
            sectors.extend(lx.sector_of_symbol(e["symbol"]))
    sectors = sorted(set(sectors))

    rw = float(region_weights.get(item.region, 1.0))
    topic_hits = len(topics)
    heat = round((abs(score) * 2.0 + topic_hits * 1.5 + 1.0) * rw * 10, 2)
    if item.kind in ("influencer", "blog"):
        heat = round(heat * 1.15, 2)     # 意见领袖 / 博客评论的传播权重略高

    return {
        "source_key": item.source_key, "source_name": item.source_name,
        "region": item.region, "lang": item.lang, "kind": item.kind,
        "title": item.title[:300], "url": item.url, "summary": item.summary[:600],
        "publish_time": item.publish_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "topic": topic, "topics": ",".join(topics),
        "sentiment": sentiment, "sentiment_score": score, "heat": heat,
        "entities": json.dumps(entities, ensure_ascii=False),
        "sectors": ",".join(sectors),
        "_entities": entities,
    }


def _daily_series(news_rows: list[dict], days: int = 14) -> list[dict]:
    """把新闻聚合为「按日 × 区域」的情绪时间序列（含情绪指数 mood）。"""
    bucket: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"bull": 0, "bear": 0, "neutral": 0, "heat": 0.0})
    for r in news_rows:
        day = (r.get("publish_time") or "")[:10]
        if not day:
            continue
        b = bucket[(day, r.get("region") or "GLOBAL")]
        if r["sentiment"] == "看多":
            b["bull"] += 1
        elif r["sentiment"] == "看空":
            b["bear"] += 1
        else:
            b["neutral"] += 1
        b["heat"] += float(r.get("heat") or 0)

    out: list[dict] = []
    for (day, region), b in bucket.items():
        total = b["bull"] + b["bear"] + b["neutral"]
        mood = round((b["bull"] - b["bear"]) / total * 100, 1) if total else 0.0
        out.append({"day": day, "region": region, "bull": b["bull"], "bear": b["bear"],
                    "neutral": b["neutral"], "total": total,
                    "heat": round(b["heat"], 2), "mood": mood})
    out.sort(key=lambda x: (x["day"], x["region"]))
    return out


def collect(db, cfg_data: dict | None = None, enabled_only: bool = True,
            include_seed: bool = True, reset: bool = True, live: bool = True) -> dict:
    """执行一次海外舆情采集。

    reset=True 时清空海外新闻表后重建（保证情绪序列与热点不重复累加）；
    live=False 时**不联网**（仅用结构化示例条目 + 记录源状态），便于快速重载与离线运行。
    """
    data = cfg_data or load_overseas_config()
    topics_cfg = data.get("topics") or {}
    region_weights = data.get("region_weights") or {"US": 1.0, "HK": 0.85, "GLOBAL": 0.8, "CN": 0.45}

    results: list[SourceResult] = fetch_all(data, only_enabled=enabled_only) if live else []

    # 未抓取的源也记录状态（界面需如实显示「已接入 / 待激活 / 示例模式」）
    all_cfgs = sources_of(data, enabled_only=False)
    done = {r.key for r in results}
    for cfg in all_cfgs:
        if cfg.get("key") in done:
            continue
        enabled = bool(cfg.get("enabled"))
        note = cfg.get("note", "")
        if not live:
            # 示例模式：可达源标记为「示例模式·未联网」，被墙源保持「待激活」
            if enabled:
                note = "示例模式·未联网（真实抓取请点「刷新海外舆情(联网)」）"
            else:
                note = note or "未启用"
        results.append(SourceResult(
            key=cfg.get("key", ""), name=cfg.get("name", ""), region=cfg.get("region", ""),
            enabled=enabled, ok=False, requires_key=bool(cfg.get("requires_key")),
            requires_vpn=bool(cfg.get("requires_vpn")), note=note,
            custom_status=("示例模式·未联网" if (not live and enabled) else ""),
            error=note))

    if reset:
        db.clear_overseas_news()

    news_rows: list[dict] = []
    saved = 0
    for res in results:
        for item in res.items:
            row = analyze_item(item, topics_cfg, region_weights)
            cat = {k: v for k, v in row.items() if not k.startswith("_")}
            nid = db.save_overseas_news(**cat)
            for e in row["_entities"]:
                db.save_overseas_mention(nid, e["market"], e["symbol"], e["name"])
            news_rows.append(row)
            saved += 1

    # 联网源条目不足时，补充人工整理的「结构化示例」海外条目，保证两个模块可用
    seed_added = 0
    if include_seed:
        from overseas.seed import SEED_NEWS
        existing = {(r["title"], r["source_key"]) for r in news_rows}
        for item in SEED_NEWS:
            if (item.title, item.source_key) in existing:
                continue
            row = analyze_item(item, topics_cfg, region_weights)
            cat = {k: v for k, v in row.items() if not k.startswith("_")}
            nid = db.save_overseas_news(**cat)
            for e in row["_entities"]:
                db.save_overseas_mention(nid, e["market"], e["symbol"], e["name"])
            news_rows.append(row)
            seed_added += 1

    db.save_overseas_source_status([r.as_dict() for r in results])
    series = _daily_series(news_rows)
    db.replace_overseas_daily(series)

    # 生成短期热点（模块二）
    from overseas.hotspots import build_hotspots
    hotspots = build_hotspots(db, news_rows, topics_cfg)

    db.set_meta("overseas_last_collect", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return {
        "fetched": sum(len(r.items) for r in results),
        "saved": saved,
        "seed_added": seed_added,
        "days": sorted({s["day"] for s in series}),
        "hotspots": len(hotspots),
        "sources": [r.as_dict() for r in results],
    }
