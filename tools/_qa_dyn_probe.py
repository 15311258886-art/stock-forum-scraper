# -*- coding: utf-8 -*-
"""动态功能测试（端到端只读）：全站 GET 接口探测。

只发 GET；裸 socket 直连 127.0.0.1:8765（完全绕过 http_proxy，避免 502 误判）。
不写库、不改源码、不重启服务。
输出：tools/_qa_dyn_out.txt
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

HOST, PORT = "127.0.0.1", 8765
OUT = Path(__file__).resolve().parent / "_qa_dyn_out.txt"
RAW_LOG: list[str] = []


def raw_get(path: str, timeout: float = 20.0) -> dict:
    """裸 socket GET，返回 status/headers/body/error。"""
    req = (f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"Connection: close\r\nUser-Agent: qa-dyn-probe\r\n\r\n")
    res: dict = {"path": path, "status": None, "headers": {}, "body": b"",
                 "error": None, "status_line": ""}
    try:
        s = socket.create_connection((HOST, PORT), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(req.encode("utf-8", "surrogateescape"))
        chunks = []
        while True:
            d = s.recv(65536)
            if not d:
                break
            chunks.append(d)
        s.close()
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    raw = b"".join(chunks)
    if not raw:
        res["error"] = "EMPTY_REPLY: 服务器未返回任何字节（连接被直接关闭）"
        return res
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        res["error"] = f"不完整响应（{len(raw)} 字节，无 HTTP 头终止符）"
        res["body"] = raw
        return res
    head = raw[:sep].decode("iso-8859-1")
    body = raw[sep + 4:]
    lines = head.split("\r\n")
    res["status_line"] = lines[0]
    try:
        res["status"] = int(lines[0].split(" ")[1])
    except Exception:  # noqa: BLE001
        pass
    hdrs = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    res["headers"] = hdrs
    res["body"] = body
    return res


def summarize(path: str) -> dict:
    r = raw_get(path)
    body = r["body"]
    info: dict = {
        "path": path,
        "status": r["status"],
        "status_line": r["status_line"],
        "error": r["error"],
        "bytes": len(body),
        "declared_len": r["headers"].get("content-length"),
        "ctype": r["headers"].get("content-type"),
        "server": r["headers"].get("server"),
        "cors": r["headers"].get("access-control-allow-origin"),
        "json_type": None,
        "json_keys": None,
        "json_len": None,
        "item_keys": None,
        "snippet": None,
        "raw_head": None,
    }
    text = None
    try:
        text = body.decode("utf-8")
    except Exception:  # noqa: BLE001
        try:
            text = body.decode("iso-8859-1")
        except Exception:  # noqa: BLE001
            text = None
    if text is not None:
        stripped = text.lstrip()
        if stripped[:1] in "{[":
            try:
                obj = json.loads(text)
                info["json_type"] = ("dict" if isinstance(obj, dict)
                                     else ("list" if isinstance(obj, list) else type(obj).__name__))
                if isinstance(obj, dict):
                    info["json_keys"] = sorted(obj.keys())
                elif isinstance(obj, list):
                    info["json_len"] = len(obj)
                    if obj and isinstance(obj[0], dict):
                        info["item_keys"] = sorted(obj[0].keys())
                    elif obj:
                        info["item_keys"] = [f"<{type(obj[0]).__name__}>"]
                info["snippet"] = text[:400]
            except Exception as e:  # noqa: BLE001
                info["snippet"] = f"<JSON 解析失败 {type(e).__name__}: {e}> " + text[:200]
        else:
            info["snippet"] = text[:300].replace("\r\n", "\\n").replace("\n", "\\n")
            info["raw_head"] = text[:160]
    return info


def fmt_line(info: dict) -> str:
    parts = [f"{info['path']}"]
    if info["error"]:
        parts.append(f"!! {info['error']}")
    parts.append(f"status={info['status']}")
    if info["bytes"] is not None:
        parts.append(f"bytes={info['bytes']} (declared={info['declared_len']})")
    if info["ctype"]:
        parts.append(f"ctype={info['ctype']}")
    if info["json_type"]:
        t = f"json={info['json_type']}"
        if info["json_len"] is not None:
            t += f" n={info['json_len']}"
        parts.append(t)
    if info["json_keys"]:
        parts.append(f"keys={','.join(info['json_keys'])}")
    if info["item_keys"]:
        parts.append(f"item_keys={','.join(info['item_keys'])}")
    return " | ".join(parts)


PROBES = [
    # ---- 静态 ----
    "/", "/index.html",
    "/static/app.js", "/static/app.css", "/static/themes/light-tech.css",
    "/static/themes/nope.css",
    "/static/",
    "/static",
    # ---- 常规 GET 接口 ----
    "/api/summary", "/api/stocks", "/api/operating", "/api/persons",
    "/api/sentiment", "/api/topics", "/api/players",
    "/api/players?top=1", "/api/players?tier=A", "/api/players?tier=E",
    "/api/players?tier=Z", "/api/players?channel=A",
    "/api/players?channel=%27%20OR%201%3D1--",
    "/api/players?top=x&visible=1",
    "/api/overseas_sentiment", "/api/overseas_hotspots", "/api/overseas_sources",
    "/api/positions", "/api/consensus", "/api/consensus?min_holders=1",
    "/api/consensus?min_holders=1&symbol=600519",
    "/api/consensus?min_holders=1&symbol=___none",
    "/api/consensus?min_holders=abc",
    "/api/consensus?min_holders=2.5",
    "/api/consensus?min_holders=",
    "/api/consensus?min_holders=-5",
    "/api/posts", "/api/posts?limit=1", "/api/posts?limit=abc",
    "/api/posts?limit=", "/api/posts?limit=-1", "/api/posts?limit=999999999",
    "/api/login_status",
    "/api/person?name=", "/api/person?name=__nobody__",
    "/api/person?name=%E6%B5%8B%E8%AF%95",
    "/api/player?name=__nobody__", "/api/player?name=",
    "/api/stock", "/api/stock?symbol=", "/api/stock?symbol=600519",
    "/api/stock?symbol=___nope",
    "/api/stock?symbol=../../../../windows/win.ini",
    "/api/stock?symbol=%F0%9F%98%80%E6%B5%8B%E8%AF%95",
    "/api/stock?symbol=" + "A" * 10000,
    "/api/stock_names",
    "/api/detail", "/api/detail?kind=post&id=1",
    "/api/detail?kind=post&id=1&full=1",
    "/api/detail?kind=trade&id=1", "/api/detail?kind=news&id=1",
    "/api/detail?kind=bogus&id=1",
    "/api/detail?kind=post&id=abc", "/api/detail?kind=post&id=1.0",
    "/api/detail?kind=post&id=-1", "/api/detail?kind=post&id=999999999",
    # ---- 路径与越界 ----
    "/api/nonexistent", "/api/summary/", "/API/summary", "/api/./summary",
    "/static/../app.db", "/static/%2e%2e%2fapp.db", "/static/..%2f..%2fviewer%2fapp.db",
    "/static/../server.py",
]


def main() -> None:
    log: list[str] = []
    results: dict[str, dict] = {}
    for p in PROBES:
        info = summarize(p)
        results[p] = info
        log.append(fmt_line(info))
        if info["snippet"]:
            log.append("    body[:400] = " + info["snippet"][:400].replace("\n", "\\n"))
        RAW_LOG.append(p)

    # ---------- 一致性体检 ----------
    def getjson(path: str):
        r = raw_get(path)
        try:
            return json.loads(r["body"].decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None

    log.append("")
    log.append("=" * 30 + " 一致性体检 " + "=" * 30)
    summary = getjson("/api/summary")
    stocks = getjson("/api/stocks")
    players = getjson("/api/players")
    positions = getjson("/api/positions")
    persons = getjson("/api/persons")
    topics = getjson("/api/topics")
    operating = getjson("/api/operating")
    sent = getjson("/api/sentiment")
    ov = getjson("/api/overseas_sentiment")
    hs = getjson("/api/overseas_hotspots")
    ovsrc = getjson("/api/overseas_sources")

    if summary:
        c = summary.get("counts", {})
        log.append(f"[summary.counts] {json.dumps(c, ensure_ascii=False)}")
        log.append(f"[summary] login_status 条数={len(summary.get('login_status') or [])} "
                   f"last_ingest={summary.get('last_ingest')!r} sites={summary.get('sites')}")
        pairs = [
            ("counts.positions vs len(/api/positions)", c.get("positions"), len(positions or [])),
            ("counts.players vs len(/api/players)", c.get("players"), len(players or [])),
            ("counts.persons vs len(/api/persons)", c.get("persons"), len(persons or [])),
            ("counts.topics vs len(/api/topics)", c.get("topics"), len(topics or [])),
            ("counts.posts vs sum(/api/sentiment.distribution)", c.get("posts"),
             sum((sent or {}).get("distribution", {}).values()) if sent else None),
            ("counts.stock_mentions vs sum(sentiment.map.mentions)", c.get("stock_mentions"),
             sum(x.get("mentions", 0) for x in (sent or {}).get("map", [])) if sent else None),
            ("counts.overseas_news vs /api/overseas_sentiment.total", c.get("overseas_news"),
             (ov or {}).get("total")),
            ("counts.overseas_hotspots vs len(/api/overseas_hotspots)", c.get("overseas_hotspots"),
             len(hs or [])),
        ]
        for label, a, b in pairs:
            mark = "OK" if a == b else "MISMATCH"
            log.append(f"  {mark}: {label}  ({a} vs {b})")
        log.append(f"[len] stocks={len(stocks or [])} operating={len(operating or [])} "
                   f"(api_operating LIMIT 200) persons={len(persons or [])} (LIMIT 100) "
                   f"topics={len(topics or [])} (LIMIT 30)")

    if sent:
        d = sent.get("distribution", {})
        log.append(f"[sentiment] distribution={json.dumps(d, ensure_ascii=False)} "
                   f"map_len={len(sent.get('map', []))} name_covered={sent.get('name_covered')}/"
                   f"{sent.get('name_total')}  (注意 map 仅取前 30)")

    if ov:
        dist = ov.get("distribution", {})
        reg = ov.get("region_dist", {})
        smix = ov.get("source_mix", [])
        log.append(f"[overseas] total={ov.get('total')} net_mood={ov.get('net_mood')} "
                   f"heat_total={ov.get('heat_total')}")
        log.append(f"  distribution={json.dumps(dist, ensure_ascii=False)} "
                   f"sum={sum(dist.values())}")
        recalc_net = round((dist.get('看多', 0) - dist.get('看空', 0)) / sum(dist.values()) * 100, 1) \
            if sum(dist.values()) else 0.0
        log.append(f"  净情绪重算={recalc_net}（接口给 {ov.get('net_mood')}） -> "
                   f"{'OK' if recalc_net == ov.get('net_mood') else 'MISMATCH'}")
        log.append(f"  region_dist={json.dumps(reg, ensure_ascii=False)} "
                   f"sum(count)={sum(v.get('count', 0) for v in reg.values())}")
        log.append(f"  source_mix 条数={len(smix)} sum(n)={sum(s.get('n', 0) for s in smix)} "
                   f"（应等于 total）")
        log.append(f"  daily 行数={len(ov.get('daily') or [])} news 条数={len(ov.get('news') or [])}"
                   f" symbols={len(ov.get('symbols') or [])} region_weights={ov.get('region_weights')}")
        log.append(f"  topics={ov.get('topics')}")
        heats = [v.get('heat', 0) for v in reg.values()]
        log.append(f"  SUM(region_dist.heat)={round(sum(heats), 2)} vs heat_total={ov.get('heat_total')}")

    if ovsrc:
        st = ovsrc.get("sources", [])
        statuses: dict = {}
        for s in st:
            statuses[s.get("status")] = statuses.get(s.get("status"), 0) + 1
        log.append(f"[overseas_sources] 条数={len(st)} status 分布={json.dumps(statuses, ensure_ascii=False)}")
        log.append(f"  字段={sorted(st[0].keys()) if st else '无'}")

    if hs:
        log.append(f"[overseas_hotspots] 条数={len(hs)}")
        for h in hs[:20]:
            fields = ("id", "title", "direction", "heat", "amplification_factor", "confidence")
            log.append("  - " + " | ".join(f"{k}={h.get(k)!r}" for k in fields))
            for k in ("decision", "overview", "variables", "sectors_up_detail",
                      "sectors_down_detail", "sectors_up", "sectors_down",
                      "domestic_sectors", "driver_sources", "driver_regions",
                      "evidence", "related_symbols"):
                v = h.get(k)
                t = type(v).__name__ + (f"(n={len(v)})" if isinstance(v, (list, str)) else "")
                log.append(f"      {k}: {t} = "
                           f"{(json.dumps(v, ensure_ascii=False)[:200] if v is not None else 'None')}")

    # 单只股票交叉校验
    log.append("")
    log.append("=" * 30 + " 单只股票交叉校验 " + "=" * 30)
    syms = [x.get("symbol") for x in (stocks or [])][:3]
    for sym in syms:
        d = getjson(f"/api/stock?symbol={sym}")
        cons = getjson(f"/api/consensus?min_holders=1&symbol={sym}")
        pos_all = [p for p in (positions or []) if p.get("symbol") == sym]
        if not d:
            log.append(f"[{sym}] /api/stock 无响应")
            continue
        log.append(f"[{sym}] name={d.get('symbol_name')!r} company={d.get('company')!r} "
                   f"market={d.get('market')!r} name_missing={d.get('name_missing')}")
        log.append(f"    mention_count={d.get('mention_count')} len(mentions)={len(d.get('mentions') or [])} "
                   f"| trade_count={d.get('trade_count')} len(trades)={len(d.get('trades') or [])} "
                   f"| position_count={d.get('position_count')} len(positions)={len(d.get('positions') or [])} "
                   f"| holders={d.get('holders')}")
        log.append(f"    len(/api/positions 该 symbol)={len(pos_all)} "
                   f"distinct players={len({p.get('player') for p in pos_all})} "
                   f"| /api/consensus(min=1) holders={cons[0].get('holders') if cons else None}")
        vs = d.get("views") or {}
        log.append(f"    view_summary={d.get('view_summary')} "
                   f"len(bull/bear/neu)={len(vs.get('bull', []))}/{len(vs.get('bear', []))}/{len(vs.get('neutral', []))}")
        # 持仓起始日 + 持有天数
        for p in (d.get("positions") or [])[:3]:
            log.append(f"    pos: {p.get('player')} ratio={p.get('ratio')} hold_since={p.get('hold_since')!r} "
                       f"hold_days={p.get('hold_days')} buy_price={p.get('buy_price')} "
                       f"current_price={p.get('current_price')} action={p.get('action')} "
                       f"access={p.get('access_level')}")

    log.append("")
    log.append("=" * 30 + " 接口字段 × 前端读取 对账 " + "=" * 30)
    # 前端真正读取的字段（人工从 app.js 提取）
    expect = {
        "/api/summary": ["counts", "login_status", "last_ingest", "sites",
                         "counts.raw_pages", "counts.posts", "counts.trades", "counts.news",
                         "counts.players", "counts.positions", "counts.persons",
                         "counts.stock_mentions", "counts.topics"],
        "/api/stocks": ["symbol", "symbol_name", "name", "company", "market", "name_missing",
                        "mentions", "last_time"],
        "/api/sentiment": ["distribution", "map", "name_covered", "name_total",
                           "map.symbol", "map.name", "map.company", "map.market",
                           "map.name_missing", "map.bull", "map.bear", "map.neutral", "map.mentions"],
        "/api/operating": ["id", "author", "post_time", "action", "symbol", "symbol_name",
                           "price", "quantity", "author_platform", "author_tier", "site"],
        "/api/persons": ["name", "mention_count", "role", "sites", "tier", "platform"],
        "/api/topics": ["topic", "weight"],
        "/api/players": ["rank_position", "name", "tier", "channel", "ranking_score",
                         "influence", "is_top_player", "source_url"],
        "/api/consensus": ["symbol", "symbol_name", "company", "market", "name_missing",
                           "holders", "weighted_holders", "tiers", "channels",
                           "discuss_heat", "platforms", "players", "sources",
                           "players.channel", "players.player", "players.action",
                           "players.ratio", "players.site", "players.source_url"],
        "/api/positions": ["site", "player", "channel", "symbol", "symbol_name", "ratio",
                           "action", "post_url"],
        "/api/stock": ["symbol", "symbol_name", "company", "market", "name_missing",
                       "mentions", "trades", "positions", "holders", "mention_count",
                       "trade_count", "position_count", "views", "view_summary"],
        "/api/detail": ["id", "site", "author", "post_id", "post_url", "post_time", "text",
                        "sentiment", "sentiment_score", "raw_html"],
        "/api/person": ["name", "meta", "posts", "trades", "positions", "mentioned_stocks",
                        "post_count", "trade_count", "position_count"],
        "/api/overseas_sentiment": ["distribution", "net_mood", "heat_total", "total",
                                    "region_dist", "daily", "source_mix", "news", "symbols",
                                    "region_weights", "topics",
                                    "region_dist.*.count", "region_dist.*.heat",
                                    "source_mix.*.source_name", "source_mix.*.region",
                                    "source_mix.*.n", "news.*.region", "news.*.source_name",
                                    "news.*.publish_time", "news.*.title", "news.*.url",
                                    "news.*.topic", "news.*.sectors",
                                    "daily.*.day", "daily.*.region", "daily.*.mood",
                                    "symbols.*.symbol", "symbols.*.name"],
        "/api/overseas_hotspots": ["direction", "domestic_sectors", "driver_sources",
                                   "driver_regions", "decision", "overview", "variables",
                                   "sectors_up_detail", "sectors_down_detail", "sectors_up",
                                   "sectors_down", "title", "amplification_factor",
                                   "confidence", "heat", "transmission", "amplification",
                                   "cross_check"],
        "/api/overseas_sources": ["sources", "region_weights", "topics",
                                  "sources.*.name", "sources.*.status", "sources.*.region",
                                  "sources.*.requires_vpn", "sources.*.requires_key",
                                  "sources.*.count"],
    }
    for path, fields in expect.items():
        r = raw_get(path)
        try:
            obj = json.loads(r["body"].decode("utf-8"))
        except Exception:  # noqa: BLE001
            log.append(f"  {path}: 无法解析 JSON -> 跳过")
            continue
        if isinstance(obj, list):
            keys = set(obj[0].keys()) if obj and isinstance(obj[0], dict) else set()
        else:
            keys = set(obj.keys())
        # 顶层字段 + 一层嵌套
        nested: set = set()
        for k, v in (obj.items() if isinstance(obj, dict) else []):
            if isinstance(v, list) and v and isinstance(v[0], dict):
                nested |= {f"{k}.{n}" for n in v[0].keys()}
            if isinstance(v, dict):
                for kk in v.keys():
                    nested.add(f"{k}.{kk}")
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            for k, v in obj[0].items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    nested |= {f"{k}.{n}" for n in v[0].keys()}
        avail = keys | nested
        missing = []
        for f in fields:
            if "." in f:
                ok = f in avail
            else:
                ok = f in keys
            if not ok:
                missing.append(f)
        log.append(f"  {path}: 前端需要 {len(fields)} 个字段，缺失 {missing if missing else '无'}")

    OUT.write_text("\n".join(log), encoding="utf-8")
    print("WROTE", OUT)


if __name__ == "__main__":
    main()
