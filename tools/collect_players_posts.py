# -*- coding: utf-8 -*-
r"""全量采集选手个人主页帖子原文 → 暂存区（只采素材，不写库、不回填交易）。

【产出目录】F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\
  players_posts.jsonl       一帖一行（site/nickname/home_url/title/publish_time/post_url/text/text_kind）
  players_posts_summary.csv 每人概览（成功/失败/条数）
  collect_players.log       进度日志（实时可看）
  players_home_ledger.jsonl 断点台账（重跑跳过已抓主页）

【阶段】
  Phase 1：sina 52 + xueqiu 33 = 85 人，免登录，每人 1 次列表请求。
  Phase 2：taoguba 139 人，主页只有标题+时间，正文须逐篇抓 /a/<id> 详情页。

【硬约束】不写 viewer/app.db；不改 config/sites.yaml；≤10 req/min；失败指数退避；断点续抓；
         抓不到如实记失败，不硬凑；sina 只有摘要 → text_kind=summary（不丢也不编）。

【用法】
  # 全量 Phase 1（sina + 雪球）
  .venv/Scripts/python.exe tools/collect_players_posts.py --phase 1
  # 先小样验证
  .venv/Scripts/python.exe tools/collect_players_posts.py --phase 1 --limit 5
  # Phase 2（淘股吧，正文逐篇抓）
  .venv/Scripts/python.exe tools/collect_players_posts.py --phase 2 --detail 5
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 绕过本机全局代理：direct 连接，绝不发往 127.0.0.1 代理
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper import players_home  # noqa: E402
from scraper.config import get_site, load_config  # noqa: E402
from scraper.parsers.xueqiu import XueqiuParser  # noqa: E402
from scraper.session import RateLimitedSession  # noqa: E402

CONFIG = str(ROOT / "config" / "sites_players.yaml")
CSV = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\名单B_选手主页URL.csv"
OUTDIR = Path(r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据")
POSTS = OUTDIR / "players_posts.jsonl"
SUMMARY = OUTDIR / "players_posts_summary.csv"
LOG = OUTDIR / "collect_players.log"
LEDGER = OUTDIR / "players_home_ledger.jsonl"

RATE_PER_MIN = 10          # ≤10 req/min（克制）
SINA_INTERVAL = 60.0 / RATE_PER_MIN
XQ_INTERVAL = 60.0 / RATE_PER_MIN
MAX_RETRY = 3              # 失败重试次数（指数退避）


# ---------------------------------------------------------------- 日志
def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- 装载/断点
def load_players(site_map: dict) -> dict:
    grouped: dict[str, list[dict]] = {}
    with open(CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            key = site_map.get((r.get("来源站点") or "").strip())
            if not key:
                continue
            grouped.setdefault(key, []).append({
                "name": (r.get("选手昵称") or "").strip(),
                "level": (r.get("层级") or "").strip(),
                "head": (r.get("是否头部") or "").strip(),
                "url": (r.get("论坛主页URL") or "").strip(),
            })
    for lst in grouped.values():
        lst.sort(key=lambda x: 0 if (x["level"] == "A" or x["head"] == "是") else 1)
    return grouped


def load_done_home() -> set[str]:
    """已成功抓取的主页 URL（断点续抓：重跑跳过）。

    只把 status=ok 视为完成；empty 视为**可重试**——雪球浏览器渲染有偶发超时
    （同一批人两次跑，返回 0 条的人并不相同），重跑会自动补抓这些空页。
    """
    done: set[str] = set()
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "ok":
                done.add(rec.get("home_url", ""))
    return done


def load_seen_posts() -> set[str]:
    """已落盘的帖子 post_url（跨站去重，避免重复行）。"""
    seen: set[str] = set()
    if POSTS.exists():
        for line in POSTS.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("post_url"):
                seen.add(rec["post_url"])
    return seen


def append_posts(records: list[dict]) -> int:
    if not records:
        return 0
    with open(POSTS, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)


def append_ledger(rec: dict) -> None:
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def post_record(site, nickname, home_url, title, publish_time, post_url, text, text_kind):
    return {"site": site, "nickname": nickname, "home_url": home_url, "title": title or "",
            "publish_time": publish_time or "", "post_url": post_url or "",
            "text": text or "", "text_kind": text_kind}


def reset_site_entries(sites: set[str]) -> tuple[int, int]:
    """从 posts.jsonl 与 ledger.jsonl 中删除指定站点条目（重抓前清场，保证字段可刷新）。

    返回 (删除的帖子行数, 删除的台账行数)。
    """
    def _filter(path: Path) -> int:
        if not path.exists():
            return 0
        dropped = 0
        keep: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                keep.append(line)
                continue
            if rec.get("site") in sites:
                dropped += 1
            else:
                keep.append(line)
        path.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        return dropped
    return _filter(POSTS), _filter(LEDGER)


# ---------------------------------------------------------------- 抓取（带退避）
def fetch_with_backoff(fn, url: str, label: str, retry: int = MAX_RETRY):
    """指数退避重试；全失败抛异常（由调用方记账）。"""
    last = None
    for i in range(retry):
        try:
            return fn(url)
        except Exception as e:  # noqa: BLE001
            last = e
            wait = 3 * (3 ** i)          # 3s, 9s, 27s
            log(f"  ! {label} 第{i+1}次失败：{e} → 退避 {wait}s")
            time.sleep(wait)
    raise last


# ---------------------------------------------------------------- Phase 1: sina
def collect_sina(cfg, players, done_home, seen_posts, limit):
    sess = RateLimitedSession(user_agent=cfg.get("user_agent", ""),
                              rate_limit_per_min=RATE_PER_MIN, timeout=cfg.get("timeout", 20),
                              retry_max=3, respect_robots=bool(cfg.get("respect_robots", True)),
                              extra_headers=cfg.get("extra_headers") or None)
    sess.session.trust_env = False
    results = []
    n = 0
    for pl in players:
        if limit and n >= limit:
            break
        n += 1
        url = pl["url"]
        if url in done_home:
            log(f"[sina] 跳过（已抓）{pl['name']}")
            continue
        rec = {"site": "sina", "nickname": pl["name"], "home_url": url,
               "http_status": "", "n_posts": 0, "status": "", "note": ""}
        t0 = time.time()
        try:
            resp = fetch_with_backoff(sess.get, url, f"sina/{pl['name']}", )
            if (resp.encoding or "").lower() == "iso-8859-1":
                resp.encoding = "gb18030"
            rec["http_status"] = str(resp.status_code)
            posts = list(players_home.parse_home("sina", resp.text, url, cfg))
            records = []
            for p in posts:
                pu = p.get("post_url")
                if not pu or pu in seen_posts:
                    continue
                seen_posts.add(pu)
                records.append(post_record(
                    "sina", pl["name"], url, p.get("title"), p.get("post_time"),
                    pu, p.get("text"), "summary"))   # 列表页只有摘要 → summary（如实）
            rec["n_posts"] = append_posts(records)
            rec["status"] = "ok" if records else "empty"
            rec["note"] = f"主贴 {len(posts)} 条，新增 {rec['n_posts']}"
        except Exception as e:  # noqa: BLE001
            rec["status"] = "error"
            rec["note"] = str(e)[:200]
        append_ledger(rec)
        results.append(rec)
        log(f"[sina] {pl['name']:<12} HTTP={rec['http_status']} 新增={rec['n_posts']} "
            f"耗时={time.time()-t0:.1f}s status={rec['status']}")
    return results


# ---------------------------------------------------------------- Phase 1: xueqiu
def collect_xueqiu(cfg, players, done_home, seen_posts, limit):
    from scraper.browser_fetch import browser_get
    parser = XueqiuParser(cfg)
    results = []
    n = 0
    for pl in players:
        if limit and n >= limit:
            break
        n += 1
        url = pl["url"]
        if url in done_home:
            log(f"[xueqiu] 跳过（已抓）{pl['name']}")
            continue
        rec = {"site": "xueqiu", "nickname": pl["name"], "home_url": url,
               "http_status": "", "n_posts": 0, "status": "", "note": ""}
        t0 = time.time()
        try:
            resp = fetch_with_backoff(browser_get, url, f"xueqiu/{pl['name']}")
            rec["http_status"] = str(resp.status_code)
            blocked = parser._is_blocked(resp.text)
            posts = [] if blocked else list(parser.parse_posts(resp.text, url))
            records = []
            for p in posts:
                pu = p.get("post_url")
                if not pu or pu in seen_posts or pu == "https://xueqiu.com":
                    continue
                seen_posts.add(pu)
                records.append(post_record(
                    "xueqiu", pl["name"], url, p.get("title") or "", p.get("post_time"),
                    pu, p.get("text"), "full"))      # 雪球时间线为正文全文
            rec["n_posts"] = append_posts(records)
            if blocked:
                rec["status"] = "error"
                rec["note"] = "WAF/滑块拦截"
            else:
                rec["status"] = "ok" if records else "empty"
                rec["note"] = f"时间线 {len(posts)} 条，新增 {rec['n_posts']}"
        except Exception as e:  # noqa: BLE001
            rec["status"] = "error"
            rec["note"] = str(e)[:200]
        append_ledger(rec)
        results.append(rec)
        log(f"[xueqiu] {pl['name']:<12} HTTP={rec['http_status']} 新增={rec['n_posts']} "
            f"耗时={time.time()-t0:.1f}s status={rec['status']}")
        time.sleep(XQ_INTERVAL)          # 雪球也克制：每次之间间隔
    return results


# ---------------------------------------------------------------- Phase 2: taoguba
def collect_taoguba(cfg, players, done_home, seen_posts, limit, detail, deadline=None):
    """淘股吧：主页只有标题+时间，正文须逐篇抓 /a/<id> 详情页。

    长任务纪律：
    - **每篇详情抓到即落盘**（append_posts 单条写入），进程被杀也不丢已抓正文；
    - 主页级用 done_home 台账跳过已完成的人；详情级用 seen_posts(post_url) 去重，
      同一人中断后重跑只补未抓的详情，不重复下载；
    - deadline 到点后**在整人边界优雅停止**，不打断正在写的人。
    """
    from scraper.auth import ensure_login
    sess = RateLimitedSession(user_agent=cfg.get("user_agent", ""),
                              rate_limit_per_min=RATE_PER_MIN, timeout=cfg.get("timeout", 20),
                              retry_max=3, respect_robots=False)
    sess.session.trust_env = False
    method = ensure_login(sess, cfg)
    log(f"[taoguba] 登录方式={method}")
    results = []
    n = 0
    stopped = False
    for pl in players:
        if limit and n >= limit:
            break
        if deadline and time.time() >= deadline:
            log(f"[taoguba] 达到时间上限，优雅停止（本轮已处理到第 {n} 人）")
            stopped = True
            break
        n += 1
        url = pl["url"]
        if url in done_home:
            log(f"[taoguba] 跳过（已抓）{pl['name']}")
            continue
        rec = {"site": "taoguba", "nickname": pl["name"], "home_url": url,
               "http_status": "", "n_posts": 0, "status": "", "note": ""}
        t0 = time.time()
        new = dfail = 0
        try:
            resp = fetch_with_backoff(sess.get, url, f"taoguba/{pl['name']}")
            rec["http_status"] = str(resp.status_code)
            blog = list(players_home.parse_home("taoguba", resp.text, url, cfg))
            for p in blog[:detail]:
                pu = p.get("post_url")
                if not pu or pu in seen_posts:
                    continue
                try:
                    dr = fetch_with_backoff(sess.get, pu, f"taoguba-detail/{p.get('post_id')}")
                except Exception as e:  # noqa: BLE001
                    dfail += 1
                    log(f"  ! 详情失败 {pu}: {e}")
                    continue
                art = players_home.parse_taoguba_article(dr.text, cfg.get("home_selectors"))
                if not art:
                    dfail += 1
                    log(f"  ! 详情无正文 {pu}")
                    continue
                tm = players_home.taoguba_article_time(dr.text) or p.get("post_time")
                seen_posts.add(pu)
                # 立即落盘（关键：中断不丢已抓正文）
                append_posts([post_record(
                    "taoguba", pl["name"], url, art.get("title") or p.get("title"),
                    tm, pu, art.get("body", ""), "full")])
                new += 1
            rec["n_posts"] = new
            if not blog:
                rec["status"] = "empty"
            elif new > 0:
                rec["status"] = "ok"
            elif dfail > 0:
                rec["status"] = "partial"
            else:
                rec["status"] = "ok"      # 该人详情此前已抓全，本轮无新增
            rec["note"] = (f"博客列表 {len(blog)} 篇，新增正文 {new} 篇"
                           + (f"，详情失败 {dfail}" if dfail else ""))
        except Exception as e:  # noqa: BLE001
            rec["status"] = "error"
            rec["note"] = str(e)[:200]
        append_ledger(rec)
        results.append(rec)
        log(f"[taoguba] {pl['name']:<12} HTTP={rec['http_status']} 新增={rec['n_posts']} "
            f"耗时={time.time()-t0:.1f}s status={rec['status']}")
    if stopped:
        log("[taoguba] 分段运行结束：再执行同一命令即可从断点继续（已抓不重复）")
    return results


# ---------------------------------------------------------------- 汇总
def write_summary(all_results: list[dict]) -> None:
    """把台账全量（含历史运行）汇总为每人一行 CSV。"""
    rows: dict[tuple, dict] = {}
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows[(r.get("site"), r.get("home_url"))] = r   # 后者覆盖前者（最新一次为准）
    with open(SUMMARY, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["site", "nickname", "home_url", "http_status", "status",
                    "n_posts", "note"])
        for r in sorted(rows.values(), key=lambda x: (x.get("site", ""), x.get("nickname", ""))):
            w.writerow([r.get("site"), r.get("nickname"), r.get("home_url"),
                        r.get("http_status"), r.get("status"), r.get("n_posts"),
                        r.get("note", "")])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全量采集选手主页帖子原文 → 暂存区")
    ap.add_argument("--phase", type=int, choices=[1, 2], default=1)
    ap.add_argument("--site", nargs="+", default=None)
    ap.add_argument("--limit", type=int, default=0, help="每站人数上限（0=不限）")
    ap.add_argument("--detail", type=int, default=5, help="Phase2 淘股吧每人取正文的详情页数")
    ap.add_argument("--reset-site", nargs="+", default=None,
                    help="重抓前清空这些站点已落盘条目（posts+ledger），保证字段可刷新")
    ap.add_argument("--max-seconds", type=int, default=0,
                    help="本轮最长运行秒数（0=不限）；到点后在整人边界优雅停止")
    ap.add_argument("--rate", type=int, default=0,
                    help="本轮流控上限（req/min，0=用默认 10）；补抓被风控的人时建议降到 5")
    ap.add_argument("--cold-start", type=int, default=0,
                    help="抓取前先静默等待的秒数（换新会话/冷启动，让站点风控冷却）")
    ap.add_argument("--names", nargs="+", default=None,
                    help="仅处理这些昵称（单点补抓用，如 3 个 partial）")
    args = ap.parse_args(argv)

    # 运行期覆盖流控与冷启动（不改变默认行为）
    global RATE_PER_MIN, SINA_INTERVAL, XQ_INTERVAL
    if args.rate and args.rate > 0:
        RATE_PER_MIN = args.rate
        SINA_INTERVAL = 60.0 / RATE_PER_MIN
        XQ_INTERVAL = 60.0 / RATE_PER_MIN
    if args.cold_start > 0:
        log(f"冷启动：抓取前静默等待 {args.cold_start}s（风控冷却）")
        time.sleep(args.cold_start)

    deadline = (time.time() + args.max_seconds) if args.max_seconds else None

    OUTDIR.mkdir(parents=True, exist_ok=True)
    data = load_config(CONFIG)

    if args.reset_site:
        dp, dl = reset_site_entries(set(args.reset_site))
        log(f"重置：从 posts 删除 {dp} 行、ledger 删除 {dl} 行（站点={args.reset_site}）")

    site_map = (data.get("player_sources", {}) or {}).get("site_map") or {
        "taoguba": "taoguba", "sina": "sina", "xueqiu": "xueqiu"}
    grouped = load_players(site_map)
    if args.names:
        wanted = set(args.names)
        for k in list(grouped.keys()):
            grouped[k] = [p for p in grouped[k] if p["name"] in wanted]
        log(f"单点补抓：仅处理 {sorted(wanted)}")
    done_home = load_done_home()
    seen_posts = load_seen_posts()

    if args.site:
        sites = args.site
    else:
        sites = ["sina", "xueqiu"] if args.phase == 1 else ["taoguba"]

    log(f"==== 开始 Phase {args.phase}，站点={sites}，limit={args.limit or '不限'}，"
        f"已抓主页={len(done_home)}，已落帖子={len(seen_posts)} ====")

    all_results: list[dict] = []
    for site in sites:
        cfg = get_site(data, site)
        cfg["_key"] = site
        players = grouped.get(site, [])
        log(f">>> {site}：名单 {len(players)} 人")
        if site == "sina":
            all_results += collect_sina(cfg, players, done_home, seen_posts, args.limit)
        elif site == "xueqiu":
            all_results += collect_xueqiu(cfg, players, done_home, seen_posts, args.limit)
        elif site == "taoguba":
            all_results += collect_taoguba(cfg, players, done_home, seen_posts,
                                           args.limit, args.detail, deadline)

    write_summary(all_results)

    # 本次运行小结
    ok = sum(1 for r in all_results if r["status"] == "ok")
    empty = sum(1 for r in all_results if r["status"] == "empty")
    err = sum(1 for r in all_results if r["status"] == "error")
    new_posts = sum(r["n_posts"] for r in all_results)
    log(f"==== 本轮结束：抓取 {len(all_results)} 人，ok={ok} empty={empty} error={err}，"
        f"新增帖子 {new_posts} ====")
    print(f"\n落盘：{POSTS}\n汇总：{SUMMARY}\n日志：{LOG}\n台账：{LEDGER}")
    print(f"本轮：ok={ok} empty={empty} error={err} 新增帖子={new_posts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
