# -*- coding: utf-8 -*-
"""按「选手个人主页」逐个采集（新增能力，复用现有采集件，最小侵入）。

【目的】验证「论坛类数据我方自己能抓」——把选手名单里的**个人主页 URL** 接进采集流程，
真实发起抓取并落**临时库**（绝不写生产库 viewer/app.db）。

【接法（二选一里的②，另有① config/sites_players.yaml 配套站点参数）】
  config/sites_players.yaml  站点参数（登录/header/限速/主页选择器），不改 sites.yaml；
  本脚本                     从名单 CSV 动态装载 227 个主页 URL，按 来源站点 分组抓取。

【三种主页形态的真实处理】
  taoguba /blog/<id>  两步：先解析博客列表（标题+时间）→ 再逐篇 /a/<id> 详情页取正文，
                      正文交给 TaogubaParser._extract_trades 抽交易（词典驱动，不猜名）。
  sina    /u/<uid>    一步：解析「主贴」div 列表 → posts（标题+摘要）。该页无个股代码，
                      交易日志为 0（诚实：不硬猜）。
  xueqiu  /u/<uid>    一步：浏览器渲染（过 WAF）→ XueqiuParser.parse_posts/parse_trades。

【用法】
  .venv/Scripts/python.exe tools/collect_players.py --site taoguba sina xueqiu --limit 3
  可选：--taoguba-detail N（每人取正文的详情页数，默认 5）、--csv、--config、
        --db _pilot_tmp.db、--json _pilot_players.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

# 绕过本机全局代理：direct 连接，绝不发往 127.0.0.1 代理
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper import players_home  # noqa: E402
from scraper.compliance import filter_public_only  # noqa: E402
from scraper.config import get_site, load_config  # noqa: E402
from scraper.parsers.taoguba import TaogubaParser  # noqa: E402
from scraper.parsers.xueqiu import XueqiuParser  # noqa: E402
from scraper.session import RateLimitedSession  # noqa: E402

DEF_CONFIG = str(ROOT / "config" / "sites_players.yaml")
DEF_CSV = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\名单B_选手主页URL.csv"

DDL = """
CREATE TABLE IF NOT EXISTS home_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site TEXT, player TEXT, author TEXT, post_id TEXT, post_url TEXT,
  post_time TEXT, title TEXT, text TEXT, UNIQUE(site, post_id)
);
CREATE TABLE IF NOT EXISTS home_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site TEXT, player TEXT, author TEXT, post_id TEXT, post_url TEXT, post_time TEXT,
  action TEXT, symbol TEXT, symbol_name TEXT, price REAL, quantity INTEGER, raw_text TEXT,
  UNIQUE(site, post_id, action, symbol_name)
);
CREATE TABLE IF NOT EXISTS home_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site TEXT, player TEXT, url TEXT, http_status TEXT, n_posts INTEGER, n_trades INTEGER,
  note TEXT, detail_fetched INTEGER
);
"""


# ---------------------------------------------------------------- 名单装载
def load_players(csv_path: str, site_map: dict) -> dict[str, list[dict]]:
    """读名单 CSV，按站点分组；优先 层级=A / 是否头部=是（排前面）。"""
    rows: list[dict] = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            site = (r.get("来源站点") or "").strip()
            key = site_map.get(site)
            if not key:            # jisilu 等无 parser 站点，跳过
                continue
            rows.append({
                "site": key, "name": (r.get("选手昵称") or "").strip(),
                "level": (r.get("层级") or "").strip(),
                "head": (r.get("是否头部") or "").strip(),
                "url": (r.get("论坛主页URL") or "").strip(),
            })
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["site"], []).append(r)
    for site, lst in grouped.items():
        lst.sort(key=lambda x: 0 if (x["level"] == "A" or x["head"] == "是") else 1)
    return grouped


# ---------------------------------------------------------------- 会话
def build_session(cfg: dict, site_key: str):
    """构造直连会话；需登录的站点（taoguba）注入本地加密 Cookie。"""
    sess = RateLimitedSession(
        user_agent=cfg.get("user_agent", ""),
        rate_limit_per_min=cfg.get("rate_limit_per_min", 15),
        timeout=cfg.get("timeout", 20),
        retry_max=cfg.get("retry_max", 3),
        respect_robots=bool(cfg.get("respect_robots", True)),
        extra_headers=cfg.get("extra_headers") or None,
    )
    sess.session.trust_env = False          # 忽略全局代理
    method = "public"
    if cfg.get("requires_login"):
        from scraper.auth import ensure_login
        method = ensure_login(sess, cfg)
    return sess, method


# ---------------------------------------------------------------- 逐站采集
def collect_taoguba(cfg: dict, players: list[dict], sess, detail_per_player: int) -> list[dict]:
    parser = TaogubaParser(cfg)
    sel = cfg.get("home_selectors")
    results = []
    for pl in players:
        url = pl["url"]
        rec = {"site": "taoguba", "player": pl["name"], "url": url,
               "http_status": "", "note": "", "detail_fetched": 0,
               "posts": [], "trades": []}
        try:
            resp = sess.get(url)
            rec["http_status"] = str(resp.status_code)
            posts = list(players_home.parse_home("taoguba", resp.text, url, cfg))
            for p in posts:
                p["player"] = pl["name"]
            rec["posts"] = posts
            rec["note"] = f"博客列表 {len(posts)} 篇"
            # 逐篇详情页取正文 → 抽交易
            for p in posts[:detail_per_player]:
                try:
                    dr = sess.get(p["post_url"])
                except Exception as e:  # noqa: BLE001
                    rec["note"] += f" | 详情失败 {p['post_id']}: {e}"[:60]
                    continue
                rec["detail_fetched"] += 1
                art = players_home.parse_taoguba_article(dr.text, sel)
                if not art:
                    continue
                tm = players_home.taoguba_article_time(dr.text) or p["post_time"]
                for t in parser._extract_trades(art["body"]):
                    rec["trades"].append({
                        "author": pl["name"], "post_id": p["post_id"], "post_url": p["post_url"],
                        "post_time": tm, "action": t.get("action"), "symbol": t.get("symbol"),
                        "symbol_name": t.get("symbol_name"), "price": t.get("price"),
                        "quantity": t.get("quantity"), "raw_text": art["body"][:500],
                    })
        except Exception as e:  # noqa: BLE001
            rec["http_status"] = rec["http_status"] or "ERR"
            rec["note"] = f"抓取异常: {e}"[:160]
        results.append(rec)
    return results


def collect_sina(cfg: dict, players: list[dict], sess) -> list[dict]:
    results = []
    for pl in players:
        url = pl["url"]
        rec = {"site": "sina", "player": pl["name"], "url": url,
               "http_status": "", "note": "", "detail_fetched": 0,
               "posts": [], "trades": []}
        try:
            resp = sess.get(url)
            if (resp.encoding or "").lower() == "iso-8859-1":
                resp.encoding = "gb18030"
            rec["http_status"] = str(resp.status_code)
            posts = list(players_home.parse_home("sina", resp.text, url, cfg))
            for p in posts:
                p["player"] = pl["name"]
            rec["posts"] = posts
            # /u/ 页无个股代码，交易日志为 0（诚实，不硬猜）
            rec["note"] = f"主贴列表 {len(posts)} 条（该页无个股代码，交易=0）"
        except Exception as e:  # noqa: BLE001
            rec["http_status"] = rec["http_status"] or "ERR"
            rec["note"] = f"抓取异常: {e}"[:160]
        results.append(rec)
    return results


def collect_xueqiu(cfg: dict, players: list[dict]) -> list[dict]:
    from scraper.browser_fetch import browser_get
    parser = XueqiuParser(cfg)
    results = []
    for pl in players:
        url = pl["url"]
        rec = {"site": "xueqiu", "player": pl["name"], "url": url,
               "http_status": "", "note": "", "detail_fetched": 0,
               "posts": [], "trades": []}
        try:
            resp = browser_get(url)
            rec["http_status"] = str(resp.status_code)
            posts = list(parser.parse_posts(resp.text, url))
            for p in posts:
                p["player"] = pl["name"]
            rec["posts"] = posts
            trades = []
            for t in parser.parse_trades(resp.text, url):
                d = t.to_dict()
                trades.append({"author": d.get("author"), "post_id": d.get("post_id"),
                               "post_url": d.get("post_url"), "post_time": d.get("post_time"),
                               "action": d.get("action"), "symbol": d.get("symbol"),
                               "symbol_name": d.get("symbol_name"), "price": d.get("price"),
                               "quantity": d.get("quantity"), "raw_text": d.get("raw_text")})
            rec["trades"] = trades
            rec["note"] = "浏览器渲染" if not parser._is_blocked(resp.text) else "WAF 拦截"
        except Exception as e:  # noqa: BLE001
            rec["http_status"] = rec["http_status"] or "ERR"
            rec["note"] = f"异常: {e}"[:160]
        results.append(rec)
    return results


# ---------------------------------------------------------------- 落库 + 报告
def save(results: list[dict], db_path: str) -> None:
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.executescript(DDL)
        for rec in results:
            con.execute(
                "INSERT OR IGNORE INTO home_runs (site, player, url, http_status, n_posts,"
                " n_trades, note, detail_fetched) VALUES (?,?,?,?,?,?,?,?)",
                (rec["site"], rec["player"], rec["url"], rec["http_status"],
                 len(rec["posts"]), len(rec["trades"]), rec["note"][:200], rec["detail_fetched"]))
            for p in rec["posts"]:
                con.execute(
                    "INSERT OR IGNORE INTO home_posts (site, player, author, post_id, post_url,"
                    " post_time, title, text) VALUES (?,?,?,?,?,?,?,?)",
                    (rec["site"], rec["player"], p.get("author", ""), p.get("post_id", ""),
                     p.get("post_url", ""), p.get("post_time", "") or "", p.get("title", ""),
                     p.get("text", "")))
            for t in filter_public_only(rec["trades"]):
                con.execute(
                    "INSERT OR IGNORE INTO home_trades (site, player, author, post_id, post_url,"
                    " post_time, action, symbol, symbol_name, price, quantity, raw_text)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rec["site"], rec["player"], t.get("author", ""), t.get("post_id", ""),
                     t.get("post_url", ""), str(t.get("post_time") or ""), t.get("action", ""),
                     t.get("symbol"), t.get("symbol_name"), t.get("price"), t.get("quantity"),
                     t.get("raw_text", "")))
        con.commit()
    finally:
        con.close()


def sample_text(rec: dict) -> str:
    if rec["posts"]:
        p = rec["posts"][0]
        t = (p.get("text") or p.get("title") or "").strip()
        return t[:120]
    return ""


def print_report(results: list[dict], db_path: str, json_path: str) -> None:
    """按「每个 URL 一行」输出试点结果表。"""
    print("\n" + "=" * 130)
    print("站点 | 昵称 | URL | HTTP状态 | posts数 | trades数 | 样例文本(截断120字)")
    print("-" * 130)
    for r in results:
        print(f"{r['site']} | {r['player']} | {r['url']} | {r['http_status']} | "
              f"{len(r['posts'])} | {len(r['trades'])} | {sample_text(r)}")
        if r.get("note"):
            print(f"       ↳ note: {r['note'][:120]}")
    print("-" * 130)
    per_site: dict[str, list[int]] = {}
    for r in results:
        a = per_site.setdefault(r["site"], [0, 0, 0])
        a[0] += 1
        a[1] += len(r["posts"])
        a[2] += len(r["trades"])
    for site, (n, p, t) in per_site.items():
        print(f"  {site}: 选手 {n} 人 → posts {p}，trades {t}")
    tot_p = sum(len(r["posts"]) for r in results)
    tot_t = sum(len(r["trades"]) for r in results)
    print(f"合计：{len(results)} 人 → posts {tot_p}，trades {tot_t}")
    print(f"落库：{db_path}（临时库，未写 viewer/app.db）  明细：{json_path}")
    print("=" * 130)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按选手个人主页逐个采集（试点）")
    ap.add_argument("--site", nargs="+", default=["taoguba", "sina", "xueqiu"],
                    choices=["taoguba", "sina", "xueqiu"])
    ap.add_argument("--limit", type=int, default=3, help="每站选手数")
    ap.add_argument("--taoguba-detail", type=int, default=5, help="淘股吧每人取正文的详情页数")
    ap.add_argument("--config", default=DEF_CONFIG)
    ap.add_argument("--csv", default="")
    ap.add_argument("--db", default=str(ROOT / "_pilot_tmp.db"))
    ap.add_argument("--json", default=str(ROOT / "_pilot_players.json"))
    ap.add_argument("--report-only", action="store_true",
                    help="不抓取，仅按已产出的 --json 重新打印结果表")
    args = ap.parse_args(argv)

    if args.report_only:
        results = json.loads(Path(args.json).read_text(encoding="utf-8"))
        print_report(results, args.db, args.json)
        return 0

    data = load_config(args.config)
    csv_path = args.csv or (data.get("player_sources", {}) or {}).get("csv") or DEF_CSV
    site_map = (data.get("player_sources", {}) or {}).get("site_map") or {
        "taoguba": "taoguba", "sina": "sina", "xueqiu": "xueqiu"}

    grouped = load_players(csv_path, site_map)
    print(f"名单：{csv_path}")
    print("站点分布：" + ", ".join(f"{k}={len(v)}" for k, v in sorted(grouped.items())))

    all_results: list[dict] = []
    for site in args.site:
        cfg = get_site(data, site)
        cfg["_key"] = site
        players = grouped.get(site, [])[:args.limit]
        print(f"\n>>> 采集 {site}：{len(players)} 人 " +
              " | ".join(f"{p['name']}({p['level']}/{p['head']})" for p in players))
        if site == "xueqiu":
            all_results += collect_xueqiu(cfg, players)
        else:
            sess, method = build_session(cfg, site)
            print(f"    会话：{method}")
            if site == "taoguba":
                all_results += collect_taoguba(cfg, players, sess, args.taoguba_detail)
            else:
                all_results += collect_sina(cfg, players, sess)

    save(all_results, args.db)
    Path(args.json).write_text(json.dumps(all_results, ensure_ascii=False, indent=2),
                               encoding="utf-8")

    print_report(all_results, args.db, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
