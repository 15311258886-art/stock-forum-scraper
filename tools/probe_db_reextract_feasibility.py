# -*- coding: utf-8 -*-
"""只读可行性核查：生产库 trades 的原始帖文能否拿回，用新规则重抽（换库方案①）。

严格只读：viewer/app.db 以 file:...?mode=ro 打开；不写任何库、不动 viewer/。
本脚本只读 + 计算 + 打印，输出重定向到 tools/_recosver_probe.log。
"""
from __future__ import annotations
import collections, json, os, sqlite3, sys
from pathlib import Path
import os

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = Path(os.environ.get("VIEWER_DB") or (ROOT / "viewer" / "app.db"))
URI = "file:%s?mode=ro" % str(DB).replace("\\", "/")

from scraper.config import get_site, load_config  # noqa: E402
from scraper.parsers.taoguba import TaogubaParser  # noqa: E402
from scraper.parsers.xueqiu import XueqiuParser  # noqa: E402


def wrap_taoguba(author, text, url):
    return ('<div class="topic-new"><a href="/blog/1"><span>%s</span></a></div>'
            '<a href="%s"><p class="topic-info">%s</p></a>'
            '<div class="topic-end"><span> · 1阅读数 · 09-24 15:54</span></div>'
            % (author or "", url or "/a/x", text or ""))


def wrap_xueqiu(author, text):
    return ('<article class="timeline__item"><a class="user-name">%s</a>'
            '<div class="content content--description">%s</div>'
            '<div class="timeline__item__info">09-24 15:54</div></article>'
            % (author or "", text or ""))


def main():
    # 直接以 UTF-8 写日志（绕开 Windows 控制台编码把中文写花）
    log = open(ROOT / "tools" / "_db_reextract_probe.log", "w", encoding="utf-8")
    sys.stdout = log

    con = sqlite3.connect(URI, uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    print("=" * 78)
    print("[1] trades 表原始帖文可得性")
    print("=" * 78)
    cols = [r[1] for r in cur.execute("PRAGMA table_info(trades)")]
    print("trades 列:", cols)
    n = cur.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
    n_pid = cur.execute(
        "SELECT COUNT(*) c FROM trades WHERE post_id IS NOT NULL AND post_id<>''").fetchone()["c"]
    n_raw = cur.execute(
        "SELECT COUNT(*) c FROM trades WHERE raw_text IS NOT NULL AND raw_text<>''").fetchone()["c"]
    n_both = cur.execute(
        "SELECT COUNT(*) c FROM trades WHERE post_id IS NOT NULL AND post_id<>'' "
        "AND raw_text IS NOT NULL AND raw_text<>''").fetchone()["c"]
    n_uniq_pid = cur.execute(
        "SELECT COUNT(DISTINCT post_id) c FROM trades WHERE post_id IS NOT NULL AND post_id<>''"
    ).fetchone()["c"]
    avg_len = cur.execute(
        "SELECT AVG(LENGTH(raw_text)) a, MIN(LENGTH(raw_text)) mi, MAX(LENGTH(raw_text)) ma "
        "FROM trades WHERE raw_text IS NOT NULL AND raw_text<>''").fetchone()
    n_trunc = cur.execute("SELECT COUNT(*) c FROM trades WHERE LENGTH(raw_text)>=490").fetchone()["c"]
    print(f"trades 总行数 = {n}")
    print(f"  post_id 非空 = {n_pid}    raw_text 非空 = {n_raw}    两者都有 = {n_both}")
    print(f"  distinct post_id = {n_uniq_pid}")
    print(f"  raw_text 长度: 平均={avg_len['a']:.0f}  最小={avg_len['mi']}  最大={avg_len['ma']}")
    print(f"  raw_text 长度>=490（疑似被 500 截断）的行数 = {n_trunc} / {n_raw}")

    print("\n  样例（author / symbol / action / raw_text 前 120 字）:")
    for r in cur.execute(
        "SELECT site,author,symbol,symbol_name,action,post_id,raw_text FROM trades "
        "WHERE raw_text IS NOT NULL AND raw_text<>'' ORDER BY id LIMIT 3"):
        print("   - [%s] %s | %s %s | %s | pid=%s" %
              (r["site"], r["author"], r["symbol"], r["symbol_name"], r["action"], r["post_id"]))
        print("     raw:", (r["raw_text"] or "")[:120].replace("\n", " "))

    print("\n  各站点 post_id/raw_text 覆盖:")
    for r in cur.execute(
        "SELECT site, COUNT(*) c, SUM(post_id<>'' AND post_id IS NOT NULL) p, "
        "SUM(raw_text IS NOT NULL AND raw_text<>'') w FROM trades GROUP BY site ORDER BY c DESC"):
        print("   - %-10s 行=%3d  post_id=%3d  raw_text=%3d" %
              (r["site"], r["c"], r["p"] or 0, r["w"] or 0))

    print("\n  各站点 raw_text 长度（平均 / 最大）:")
    for r in cur.execute(
        "SELECT site, AVG(LENGTH(raw_text)) a, MAX(LENGTH(raw_text)) m, COUNT(*) c "
        "FROM trades WHERE raw_text IS NOT NULL AND raw_text<>'' GROUP BY site ORDER BY c DESC"):
        print("   - %-10s 行=%3d  平均=%.0f  最大=%d" %
              (r["site"], r["c"], r["a"] or 0, r["m"] or 0))

    print("\n" + "=" * 78)
    print("[1b] 有没有 posts 表 / 等价原帖表（退路素材）")
    print("=" * 78)
    for t in ("posts", "raw_pages"):
        tcols = [x[1] for x in cur.execute("PRAGMA table_info(%s)" % t)]
        tc = cur.execute("SELECT COUNT(*) c FROM %s" % t).fetchone()["c"]
        print("  %s: 行=%d  列=%s" % (t, tc, tcols))

    # trades 的 post_id 在 posts 表里的覆盖（posts.text 是**完整原帖**，比 raw_text 更全）
    trade_pids = [r["post_id"] for r in cur.execute(
        "SELECT DISTINCT post_id FROM trades WHERE post_id<>''")]
    cov = cur.execute(
        "SELECT COUNT(*) c FROM posts WHERE post_id IN "
        "(SELECT post_id FROM trades WHERE post_id<>'') AND text IS NOT NULL AND text<>''"
    ).fetchone()["c"]
    n_post_matched = cur.execute(
        "SELECT COUNT(DISTINCT post_id) c FROM posts WHERE post_id IN "
        "(SELECT post_id FROM trades WHERE post_id<>'')").fetchone()["c"]
    print("  → trades 的 %d 个 distinct post_id 中，posts 表可匹配到 %d 个（有非空 text 的 posts 行 %d 行）"
          % (len(trade_pids), n_post_matched, cov))
    print("  → posts.text 平均长度 = %.0f"
          % (cur.execute("SELECT AVG(LENGTH(text)) a FROM posts WHERE text<>''").fetchone()["a"] or 0))
    r = cur.execute(
        "SELECT AVG(LENGTH(text)) a, MAX(LENGTH(text)) m FROM posts WHERE text<>'' AND "
        "post_id IN (SELECT post_id FROM trades WHERE post_id<>'')").fetchone()
    print("  → 仅看 trades 命中的 post_id：posts.text 平均=%.0f 最大=%d"
          % (r["a"] or 0, r["m"] or 0))
    print("  对照样例（post_id / trades.raw_text 长 / posts.text 长）:")
    for row in cur.execute(
        "SELECT t.post_id pid, LENGTH(t.raw_text) lr, "
        "(SELECT MAX(LENGTH(text)) FROM posts p WHERE p.post_id=t.post_id) lp, t.site "
        "FROM trades t WHERE t.raw_text<>'' ORDER BY lp DESC LIMIT 6"):
        print("   - %-16s [%s] raw=%s  posts.text=%s" %
              (str(row["pid"])[:16], row["site"], row["lr"], row["lp"]))

    print("\n" + "=" * 78)
    print("[2] 小样本试算：按 post_id 归组 → 新规则重抽")
    print("=" * 78)
    data = load_config(None)
    tgb = TaogubaParser(get_site(data, "taoguba"))
    xq = XueqiuParser(get_site(data, "xueqiu"))

    # 挑 trades 最集中的 25 个 post_id（不分站点）
    pids = [r["post_id"] for r in cur.execute(
        "SELECT post_id, COUNT(*) c FROM trades WHERE post_id<>'' "
        "GROUP BY post_id ORDER BY c DESC LIMIT 25")]
    old_tot = new_tot = 0
    changed = 0
    shown = 0
    for pid in pids:
        rows = cur.execute(
            "SELECT site,author,symbol,symbol_name,action,raw_text FROM trades WHERE post_id=?",
            (pid,)).fetchall()
        old_cnt = len(rows)
        old_tot += old_cnt
        site = rows[0]["site"]
        author = rows[0]["author"]
        # raw_text 取该 post_id 下最长的一条（截断窗口最全）
        raw = max((r["raw_text"] or "" for r in rows), key=len)
        new = []
        try:
            if site == "taoguba":
                new = tgb._extract_trades(raw)
            elif site == "xueqiu":
                new = list(xq.parse_trades(wrap_xueqiu(author, raw), ""))
        except Exception as e:  # noqa: BLE001
            print("   ! %s 重抽异常: %s" % (site, e))
        new_cnt = len(new)
        new_tot += new_cnt
        if new_cnt != old_cnt:
            changed += 1
        if shown < 10 and new_cnt != old_cnt:
            shown += 1
            old_k = "%s/%s" % (rows[0]["action"], rows[0]["symbol_name"])
            print("   pid=%-14s [%s] 旧=%d 新=%d  raw[:70]=%r" %
                  (str(pid)[:14], site, old_cnt, new_cnt, raw[:70]))
    print("   ---- 25 个 post_id 合计：旧抽出 %d 条 → 新抽出 %d 条；计数变化的 post_id = %d 个" %
          (old_tot, new_tot, changed))

    print("\n" + "=" * 78)
    print("[3] 退路素材：按 sites.yaml targets 重抓的规模")
    print("=" * 78)
    try:
        cfg = load_config(None)
        for site, s in (cfg.get("sites") or {}).items():
            tg = s.get("targets") or []
            print("   - %-10s targets=%d" % (site, len(tg)))
    except Exception as e:  # noqa: BLE001
        print("   读取 sites.yaml 失败:", e)

    con.close()


if __name__ == "__main__":
    main()
