#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""雪球「人主页」实盘操作抓取（以人为轴）。

背景（2026-09-25 实测确认）：
- 王总明确要求「以实盘的人为第一抓取线索，帖子/言论作为印证」。
- 实测雪球个人主页（https://xueqiu.com/u/<uid>）的 time 线里，用户本人会写：
      2026年9月22日 于4.553位清仓 $工银蒙能清洁能源REIT(SZ180402)$
      于11.66位抢权 建仓 $平安银行(SZ000001)$
  即：真实日期 + 标的（$名称(代码)$）+ 动作（建仓/清仓/买入/卖出/加仓/减仓/抢权/持仓…）。
- 淘股吧/新浪/东财的个人页不含 $名称(代码)$ 结构，只有雪球是标准且可解析的「人轴」源。

本脚本做三件事：
  1) 逐个 uid 取主页 HTML（带本地缓存，便于离线反复调解析）
  2) 解析出 (日期, 代码, 名称, 动作, 原句) 记录
  3) 写入 app.db：positions（当前持仓口径）+ trades（操作流水），并标注来源=雪球人主页

用法：
  python xueqiu_person.py --cache            # 只抓 HTML 存缓存，不解析不入库
  python xueqiu_person.py --parse-only       # 只解析缓存（离线，秒出）
  python xueqiu_person.py --limit 3          # 抓前 3 个 uid（试跑）
  python xueqiu_person.py                    # 全量：抓+解析+入库
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

DB = os.path.join(BASE, "viewer", "app.db")
CACHE_DIR = os.path.join(BASE, "_xq_person_pages")
OUT_JSON = os.path.join(BASE, "xueqiu_person_ops.json")

# ---------- 解析规则（全部由实测样本反推，不猜） ----------

# 中文日期：2026年9月22日 / 2026年09月22日
DATE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# 标的：$名称(SZ000001)$ / $名称(SH600519)$（雪球标准写法，市场前缀可选）
STOCK_RE = re.compile(r"\$([^$()]{1,40}?)\s*[（(]\s*([Ss][HhZz])\s*(\d{6})\s*[)）]\s*\$")
# 中文逗号/句号断句
SPLIT_RE = re.compile(r"[。；;\n\r]+")

# 动作词 → 统一口径（长词优先，避免「清仓」被「仓」误伤）
ACTIONS = [
    ("清仓", "清仓"), ("卖出", "卖出"), ("sell", "卖出"),
    ("建仓", "建仓"), ("买入", "买入"), ("buy", "买入"),
    ("加仓", "加仓"), ("增持", "加仓"),
    ("减仓", "减仓"), ("减持", "减仓"),
    ("止损", "卖出"), ("止盈", "卖出"),
    ("抢权", "建仓"),
    ("补仓", "加仓"),
    ("持有", "持有"), ("持仓", "持有"),
]
ACTION_ORDER = sorted(ACTIONS, key=lambda x: -len(x[0]))


def _clean(text: str) -> str:
    """去掉 HTML 标签与实体，压缩空白。"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", text).strip()


def _norm_date(y: str, m: str, d: str) -> str:
    return "%04d-%02d-%02d" % (int(y), int(m), int(d))


def parse_page(uid: str, html: str) -> list[dict]:
    """从个人主页 HTML 解析出操作记录。

    策略：先按「时间线条目」切块，块内再按句切分；
    每句里若有 $代码$，就近找动作词（同句优先）→ 判定动作；
    句内的中文日期 + 该条目自身的日期共同决定时间。
    """
    recs: list[dict] = []
    # 按雪球的 time 线条目切块（timeline__item 是列表项容器）
    items = re.split(r'class="[^"]*timeline__item[^"]*"', html)
    if len(items) <= 1:
        items = [html]

    seen = set()
    for raw in items:
        text = _clean(raw)
        if not text or "$" not in text:
            continue
        # 该块内的中文日期（取全部，按出现顺序）
        dates = [_norm_date(*m.groups()) for m in DATE_RE.finditer(text)]
        clause_date = dates[0] if dates else None

        for clause in SPLIT_RE.split(text):
            clause = clause.strip()
            if not clause:
                continue
            stocks = list(STOCK_RE.finditer(clause))
            if not stocks:
                continue
            # 句内日期优先（比条目日期更精确）
            cd = DATE_RE.search(clause)
            d = _norm_date(*cd.groups()) if cd else clause_date
            if not d:
                continue
            # 句内动作词（取第一个命中的，长词优先）
            act = None
            for kw, norm in ACTION_ORDER:
                if kw in clause:
                    act = norm
                    break
            if not act:
                continue
            for m in stocks:
                name, mkt, code = m.group(1).strip(), m.group(2).upper(), m.group(3)
                # 名称里可能还有修饰（XD=除息），保留原名便于核对
                key = (uid, d, code, act)
                if key in seen:
                    continue
                seen.add(key)
                recs.append({
                    "uid": uid, "date": d, "code": code, "name": name,
                    "market": mkt, "action": act, "text": clause[:300],
                })
    return recs


# ---------- 抓取 ----------

def fetch_page(uid: str, timeout: int = 40, use_cache: bool = True) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    cf = os.path.join(CACHE_DIR, f"{uid}.html")
    if use_cache and os.path.exists(cf) and os.path.getsize(cf) > 5000:
        return open(cf, encoding="utf-8", errors="ignore").read()
    from scraper.browser_fetch import browser_get  # 延迟导入（需 NODE_PATH）
    url = f"https://xueqiu.com/u/{uid}"
    resp = browser_get(url, timeout=timeout)
    html = getattr(resp, "text", "") or ""
    if len(html) > 5000:
        with open(cf, "w", encoding="utf-8") as f:
            f.write(html)
    return html


def load_uids(limit: int | None = None) -> list[tuple[str, str]]:
    """从 players 表读雪球的 uid + 名字（source_url 形如 xueqiu.com/u/<uid>）。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT name, source_url FROM players WHERE site='xueqiu' "
        "AND source_url LIKE '%xueqiu.com/u/%' ORDER BY name").fetchall()
    con.close()
    out = []
    for r in rows:
        m = re.search(r"xueqiu\.com/u/(\d+)", r["source_url"] or "")
        if m:
            out.append((m.group(1), r["name"] or ""))
    if limit:
        out = out[:limit]
    return out


# ---------- 入库 ----------

def save_to_db(all_recs: dict[str, list[dict]], name_of: dict[str, str]) -> dict:
    """写入 positions / trades。positions = 每人每标的最新一次状态；trades = 全部流水。"""
    con = sqlite3.connect(DB, timeout=60)
    cur = con.cursor()
    # 备份
    bak = DB + ".bak.before-xqperson-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    con.execute("VACUUM INTO ?", (bak,)) if False else None  # 备份由调用方负责
    stat = {"positions": 0, "trades": 0, "people": 0}
    for uid, recs in all_recs.items():
        if not recs:
            continue
        player = name_of.get(uid, uid)
        stat["people"] += 1
        # 1) trades：全部流水（按 uid+日期+代码+动作 去重）
        for r in recs:
            try:
                cur.execute(
                    """INSERT OR IGNORE INTO trades
                       (site, author, post_id, post_url, post_time, action,
                        symbol, symbol_name, raw_text, access_level)
                       VALUES ('xueqiu', ?, ?, ?, ?, ?, ?, ?, ?, 'free')""",
                    (player, f"xq-{uid}-{r['date']}-{r['code']}-{r['action']}",
                     f"https://xueqiu.com/u/{uid}", r["date"] + " 00:00:00",
                     r["action"], r["code"], r["name"], r["text"]))
                stat["trades"] += cur.rowcount
            except sqlite3.Error:
                pass
        # 2) positions：取每个 (code) 的最新一条作为当前状态
        latest: dict[str, dict] = {}
        for r in recs:
            k = r["code"]
            if k not in latest or r["date"] > latest[k]["date"]:
                latest[k] = r
        for code, r in latest.items():
            if r["action"] in ("清仓", "卖出"):
                continue  # 已卖出不进当前持仓
            try:
                cur.execute(
                    """INSERT OR IGNORE INTO positions
                       (site, player, channel, symbol, symbol_name, ratio, action,
                        post_url, post_time, raw_text, access_level)
                       VALUES ('xueqiu', ?, 'D', ?, ?, NULL, ?, ?, ?, ?, 'free')""",
                    (player, code, r["name"], r["action"],
                     f"https://xueqiu.com/u/{uid}",
                     r["date"] + " 00:00:00", r["text"]))
                stat["positions"] += cur.rowcount
            except sqlite3.Error:
                pass
    con.commit()
    con.close()
    return stat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--cache", action="store_true", help="只抓 HTML 存缓存")
    ap.add_argument("--parse-only", action="store_true", help="只解析缓存，不联网")
    ap.add_argument("--no-save", action="store_true", help="不入库，只输出 JSON")
    args = ap.parse_args()

    uids = load_uids(args.limit)
    print(f"[雪球人主页] 待处理 {len(uids)} 人", flush=True)

    all_recs: dict[str, list[dict]] = {}
    name_of: dict[str, str] = {}
    for i, (uid, nm) in enumerate(uids, 1):
        name_of[uid] = nm
        try:
            html = fetch_page(uid, use_cache=args.parse_only or True)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{len(uids)}] {nm}({uid}) 抓取失败: {type(e).__name__}: {str(e)[:80]}", flush=True)
            continue
        recs = parse_page(uid, html)
        all_recs[uid] = recs
        ops = len([r for r in recs if r["action"] in ("买入", "建仓", "加仓", "抢权")])
        print(f"  [{i}/{len(uids)}] {nm:<14}({uid}) html={len(html)//1024}KB 记录{len(recs):>3} 买向{ops:>3}", flush=True)
        if not args.parse_only and not args.cache and i < len(uids):
            time.sleep(0.8)

    total = sum(len(v) for v in all_recs.values())
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "total": total, "by_uid": all_recs}, f, ensure_ascii=False, indent=1)
    print(f"\n[解析合计] {total} 条操作记录，已写 {os.path.basename(OUT_JSON)}", flush=True)

    if args.cache or args.no_save:
        return 0
    st = save_to_db(all_recs, name_of)
    print(f"[入库] players={st['people']} positions +{st['positions']} trades +{st['trades']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
