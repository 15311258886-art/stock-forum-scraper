# -*- coding: utf-8 -*-
"""回填真实发布时间（离线重放 raw_pages → 按 URL 更新 news/trades/posts 的时间）

背景：2026-09-25 修掉「用抓取时刻冒充发布时间」后，存量数据的时间被置空。
本脚本用**修好的解析器**重新解析库内已存的原始页 HTML，把能取到的真实时间回填回去。
取不到的保持空白（前端显示「未披露」），不做任何猜测。
"""
from __future__ import annotations

import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from bs4 import BeautifulSoup  # noqa: E402

from scraper.parsers.sina import SinaParser  # noqa: E402
from scraper.parsers.ths import ThsParser  # noqa: E402
from scraper.parsers.eastmoney import EastMoneyParser  # noqa: E402
from scraper.parsers.xueqiu import XueqiuParser  # noqa: E402

DB = os.path.join(BASE, "viewer", "app.db")

PARSERS = {
    "sina": (SinaParser, {}),
    "ths": (ThsParser, {}),
    "eastmoney": (EastMoneyParser, {}),
    "xueqiu": (XueqiuParser, {}),
}


def norm(v) -> str:
    """把解析器输出的时间统一成 'YYYY-MM-DD HH:MM:SS' 或 ''（库内其它站点为北京时间 naive）。"""
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    s = str(v).strip()
    if not s:
        return ""
    if len(s) == 10:          # 仅日期 → 保留日期，不补假钟点
        return s
    return s[:19]


def main() -> None:
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    pages = cur.execute(
        "SELECT site, url, html FROM raw_pages "
        "WHERE site IN ('sina','ths','eastmoney','xueqiu') AND html IS NOT NULL AND LENGTH(html) > 5000"
    ).fetchall()
    print(f"[1] 待重放原始页 {len(pages)} 个")

    news_map: dict[str, str] = {}
    trade_map: dict[str, str] = {}
    post_map: dict[str, str] = {}
    stat: dict[str, int] = {}

    for pg in pages:
        site = pg["site"]
        if site not in PARSERS:
            continue
        cls, cfg = PARSERS[site]
        try:
            parser = cls(cfg)
        except Exception as e:  # noqa: BLE001
            print(f"    初始化失败 {site}: {e}")
            continue
        html = pg["html"]
        page_url = pg["url"] or ""

        for attr, bucket in (("parse_news", news_map),
                             ("parse_trades", trade_map),
                             ("parse_posts", post_map)):
            fn = getattr(parser, attr, None)
            if fn is None:
                continue
            try:
                for it in fn(html, page_url):
                    if isinstance(it, dict):
                        url = it.get("url") or it.get("post_url") or ""
                        t = it.get("publish_time") or it.get("post_time")
                    else:
                        url = getattr(it, "url", None) or getattr(it, "post_url", None) or ""
                        t = getattr(it, "publish_time", None) or getattr(it, "post_time", None)
                    if not url:
                        continue
                    ns = norm(t)
                    if ns:
                        bucket[url] = ns
                        stat[f"{site}.{attr}"] = stat.get(f"{site}.{attr}", 0) + 1
            except Exception as e:  # noqa: BLE001
                print(f"    {site}.{attr} 异常: {type(e).__name__} {e}")

    print("[2] 重放抽到时间：")
    for k in sorted(stat):
        print(f"      {k:<24} {stat[k]} 条")
    if not (news_map or trade_map or post_map):
        print("      无（原始页可能已被风控页替换，跳过回填）")
        con.close()
        return

    # ---- 按 URL 更新 ----
    def apply(table: str, col: str, mapping: dict[str, str], url_col: str) -> tuple[int, int]:
        n, matched = 0, 0
        for url, ts in mapping.items():
            rows = cur.execute(
                f"SELECT id, {col} FROM {table} WHERE {url_col}=?", (url,)).fetchall()
            for r in rows:
                matched += 1
                if (r[col] or "") != ts:
                    cur.execute(f"UPDATE {table} SET {col}=? WHERE id=?", (ts, r["id"]))
                    n += 1
        return n, matched

    print("[3] 回填：")
    un, mn = apply("news", "publish_time", news_map, "url")
    print(f"      news.publish_time  更新 {un} 条（命中 {mn}）")
    ut, mt = apply("trades", "post_time", trade_map, "post_url")
    print(f"      trades.post_time   更新 {ut} 条（命中 {mt}）")
    up, mp = apply("posts", "post_time", post_map, "post_url")
    print(f"      posts.post_time    更新 {up} 条（命中 {mp}）")
    con.commit()

    print("[4] 回填后各站空时间占比：")
    for tbl, col in (("news", "publish_time"), ("trades", "post_time"), ("posts", "post_time")):
        for r in cur.execute(
            f"SELECT site, COUNT(*) n, SUM(CASE WHEN {col} IS NULL OR TRIM({col})='' THEN 1 ELSE 0 END) e "
            f"FROM {tbl} GROUP BY site"
        ):
            print(f"      {tbl:<7}{r['site']:<10} {r['n']-r['e']}/{r['n']} 有值")
    con.close()


if __name__ == "__main__":
    main()
