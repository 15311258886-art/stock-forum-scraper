# -*- coding: utf-8 -*-
"""C：用新语料重跑交易抽取 → staging（不碰生产库 trades 表）。

语料：采集数据/players_posts.jsonl（sina/summary · xueqiu/full · taoguba/full · image 跳过）
做法：把每篇帖子文本包成**该站解析器认识的最小 HTML**，直接调用**真实解析器**
      （TaogubaParser.parse_trades / XueqiuParser.parse_trades），因此本脚本对"改规则前/后"
      两种情况都成立——同一脚本跑两次即可得到 旧规则 vs 新规则 的对比。
基线：另从 viewer/app.db 只读读取现 trades 表（348 行）Top5，供对照。

用法：
  .venv/Scripts/python.exe tools/rerun_trades_staging.py --out <目录>\\trades_v2_before.jsonl
硬约束：只读 viewer/app.db（mode=ro）；只写 staging 文件；不改 viewer/ 任何文件。
"""
from __future__ import annotations
import argparse, collections, json, os, sqlite3, sys
from pathlib import Path

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper.config import get_site, load_config  # noqa: E402
from scraper.parsers.taoguba import TaogubaParser  # noqa: E402
from scraper.parsers.xueqiu import XueqiuParser  # noqa: E402
from scraper import bot_filter  # noqa: E402

CORPUS = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据\players_posts.jsonl"
DB = ROOT / "viewer" / "app.db"

TGB_TPL = (
    '<div class="topic-new"><a href="/blog/1"><span>{author}</span></a></div>'
    '<a href="{url}"><p class="topic-info">{text}</p></a>'
    '<div class="topic-end"><span> · 1阅读数 · {time}</span></div>'
)


def wrap_taoguba(author: str, text: str, url: str, time_str: str = "") -> str:
    # 时间用**原帖真实发布时间**（P1，2026-10-10 修：原先硬编码 "09-24 15:54"，
    # 导致全库 post_time 挤在同一刻）。time_str 为语料 publish_time，可为空串（不编造）。
    return TGB_TPL.format(author=author or "", text=text or "", url=url or "/a/x",
                          time=time_str or "")


def wrap_xueqiu(author: str, text: str, time_str: str = "") -> str:
    return (f'<article class="timeline__item"><a class="user-name">{author or ""}</a>'
            f'<div class="content content--description">{text or ""}</div>'
            f'<div class="timeline__item__info">{time_str or ""}</div></article>')


def norm_time(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


def baseline_top(db_path: Path, topn: int = 5):
    if not db_path.exists():
        return {"error": "no db"}
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    n = con.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
    rows = con.execute(
        "SELECT symbol, symbol_name, COUNT(*) c FROM trades "
        "WHERE symbol IS NOT NULL AND symbol<>'' GROUP BY symbol ORDER BY c DESC LIMIT ?",
        (topn,)).fetchall()
    bysite = con.execute("SELECT site, COUNT(*) c FROM trades GROUP BY site ORDER BY c DESC").fetchall()
    byact = con.execute("SELECT action, COUNT(*) c FROM trades GROUP BY action ORDER BY c DESC").fetchall()
    con.close()
    return {"total": n,
            "top": [(r["symbol"], r["symbol_name"], r["c"]) for r in rows],
            "by_site": [(r["site"], r["c"]) for r in bysite],
            "by_action": [(r["action"], r["c"]) for r in byact]}


def top_stats(records: list, topn: int = 5):
    cnt = collections.Counter(r["symbol"] for r in records if r.get("symbol"))
    total = sum(cnt.values())
    top = [(s, c, round(100.0 * c / total, 1) if total else 0.0) for s, c in cnt.most_common(topn)]
    return {"rows": len(records), "stocks": len(cnt), "top": top}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="staging 输出 jsonl 路径")
    ap.add_argument("--bot-filter", choices=["off", "on"], default="off",
                    help="on=应用机器资讯号过滤（丢其交易）")
    ap.add_argument("--corpus", default=CORPUS)
    args = ap.parse_args(argv)

    data = load_config(None)
    tgb = TaogubaParser(get_site(data, "taoguba"))
    xq = XueqiuParser(get_site(data, "xueqiu"))
    xq.base_url = "https://xueqiu.com"

    out = []
    skipped = collections.Counter()
    bot_dropped = collections.Counter()
    pub_by_pid = {}                      # post_id → publish_time（语料侧，量关联率用）
    with open(args.corpus, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            site, author, text = rec.get("site"), rec.get("nickname", ""), rec.get("text") or ""
            url = rec.get("post_url", "")
            # P1（2026-10-10）：post_id = 语料 post_url 的尾段（taoguba = /a/<id>；xueqiu = <status_id>）
            #   —— 既与旧 staging 的 taoguba post_id 一致，又修好 xueqiu 之前 "_<正文>" 的坏 id；
            #   post_time = 语料 publish_time（原帖真实发布时间，不编造、不硬编码）。
            pid = str(url).rsplit("/", 1)[-1]
            pub = (rec.get("publish_time") or "").strip()
            pub_by_pid[pid] = pub
            if rec.get("text_kind") == "image" or not text:
                skipped[f"{site}/no-text"] += 1
                continue
            if args.bot_filter == "on":
                is_bot, why = bot_filter.is_bot_author(author)
                if is_bot:
                    bot_dropped[author] += 1
                    continue
            trades = []
            if site == "taoguba":
                trades = list(tgb.parse_trades(wrap_taoguba(author, text, url, pub), url))
            elif site == "xueqiu":
                trades = list(xq.parse_trades(wrap_xueqiu(author, text, pub), url))
            else:
                skipped[f"{site}/no-extractor"] += 1
                continue
            for t in trades:
                d = t.to_dict()
                out.append({
                    "site": d["source_site"], "author": d["author"],
                    "post_id": pid, "post_url": url,
                    "post_time": pub,
                    "action": d["action"], "symbol": d.get("symbol"),
                    "symbol_name": d.get("symbol_name"),
                    "price": d.get("price"), "quantity": d.get("quantity"),
                    "raw_text": d.get("raw_text", ""),
                    "nickname": author, "corpus_text_kind": rec.get("text_kind"),
                })

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    st = top_stats(out)
    bysite = collections.Counter(r["site"] for r in out)
    byact = collections.Counter(r["action"] for r in out)

    print("=== 新抽取（staging） ===")
    print(f"输出文件: {args.out}")
    print(f"交易行数: {st['rows']}  涉及股票: {st['stocks']}")
    print(f"按站点: {dict(bysite)}")
    print(f"按动作: {dict(byact)}")
    print(f"Top5 股票（占比 = 该股行数 / 总行数）:")
    for s, c, pct in st["top"]:
        print(f"    {s}  {c} 条  {pct}%")

    # P1：post_time 真实性证据
    pts = [r["post_time"] for r in out]
    nonempty = [p for p in pts if p]
    uniq = sorted(set(nonempty))
    print("--- P1 post_time 证据 ---")
    print(f"distinct post_time = {len(set(pts))}（含空串） / {len(set(nonempty))}（非空）")
    print(f"非空 post_time = {len(nonempty)} / {len(pts)}")
    if uniq:
        print(f"时间范围: {uniq[0]}  ~  {uniq[-1]}")
    join_ok = sum(1 for r in out if r["post_id"] in pub_by_pid and pub_by_pid[r["post_id"]] == r["post_time"])
    print(f"post_id→publish_time 关联成功 = {join_ok} / {len(out)}  "
          f"(语料 distinct post_id={len(pub_by_pid)})")
    print(f"distinct author = {len(set(r['author'] for r in out))}")

    print(f"跳过: {dict(skipped)}")
    if bot_dropped:
        print(f"机器人过滤丢弃（作者→条数）: {dict(bot_dropped)}")

    print("\n=== 基线：现 trades 表（只读 viewer/app.db） ===")
    bs = baseline_top(DB)
    if "error" in bs:
        print(bs)
    else:
        print(f"总行数: {bs['total']}  按站点: {dict(bs['by_site'])}  按动作: {dict(bs['by_action'])}")
        tot = bs["total"] or 1
        print("Top5 股票:")
        for s, nm, c in bs["top"]:
            print(f"    {s}  {nm}  {c} 条  {round(100.0*c/tot,1)}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
