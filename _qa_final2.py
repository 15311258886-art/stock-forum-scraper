#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""换库最终核验 + WAL 检查点（不杀运行中的服务进程）。
仅对 viewer/app.db 做只读核验 + 一次安全的 wal_checkpoint(TRUNCATE)。
"""
import sqlite3, os, json, datetime

DB = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\viewer\app.db"
BAK = r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\viewer\app.db.bak-swap-20261010-000509"

def open_ro():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c

def fmt_dt(s):
    # 判定是否合法 yyyy-mm-dd hh:mm 样式
    if not s:
        return False
    try:
        datetime.datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        return True
    except Exception:
        return False

def main():
    res = {}
    c = open_ro()
    t = c.cursor()

    c1 = t.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    res["trades_rows"] = c1
    res["trades_cols"] = [r[1] for r in t.execute("PRAGMA table_info(trades)")]

    authors = t.execute("SELECT COUNT(DISTINCT author) FROM trades").fetchone()[0]
    stocks = t.execute("SELECT COUNT(DISTINCT symbol) FROM trades").fetchone()[0]
    res["distinct_authors"] = authors
    res["distinct_stocks"] = stocks

    site = dict(t.execute("SELECT site, COUNT(*) FROM trades GROUP BY site").fetchall())
    action = dict(t.execute("SELECT action, COUNT(*) FROM trades GROUP BY action").fetchall())
    res["site"] = site
    res["action"] = action

    # post_time 范围与畸形
    pt_min, pt_max = t.execute("SELECT MIN(post_time), MAX(post_time) FROM trades").fetchone()
    pt_distinct = t.execute("SELECT COUNT(DISTINCT post_time) FROM trades").fetchone()[0]
    pt_null = t.execute("SELECT COUNT(*) FROM trades WHERE post_time IS NULL OR post_time=''").fetchone()[0]
    pt_bad = t.execute("SELECT COUNT(*) FROM trades WHERE post_time IS NOT NULL AND post_time<>'' AND substr(post_time,1,1) NOT IN ('1','2')").fetchone()[0]
    res["post_time"] = {"min": pt_min, "max": pt_max, "distinct": pt_distinct, "null": pt_null, "non_yyyy_prefix": pt_bad}

    # buy_time / sell_time 填充
    buy_fill = t.execute("SELECT COUNT(*) FROM trades WHERE action IN ('BUY','HOLD') AND buy_time IS NOT NULL").fetchone()[0]
    buy_total = t.execute("SELECT COUNT(*) FROM trades WHERE action IN ('BUY','HOLD')").fetchone()[0]
    sell_fill = t.execute("SELECT COUNT(*) FROM trades WHERE action='SELL' AND sell_time IS NOT NULL").fetchone()[0]
    sell_total = t.execute("SELECT COUNT(*) FROM trades WHERE action='SELL'").fetchone()[0]
    res["buy_time_fill"] = f"{buy_fill}/{buy_total}"
    res["sell_time_fill"] = f"{sell_fill}/{sell_total}"

    # 脏名 / 机器号残留
    c000688 = t.execute("SELECT COUNT(*) FROM trades WHERE symbol='000688'").fetchone()[0]
    c000001 = t.execute("SELECT symbol_name FROM trades WHERE symbol='000001' LIMIT 1").fetchone()
    c000001_name = c000001[0] if c000001 else None
    bot = t.execute("SELECT COUNT(*) FROM trades WHERE author='远凡aa量化交易系统'").fetchone()[0]
    res["symbol_000688_rows"] = c000688
    res["symbol_000001_name"] = c000001_name
    res["bot_residue"] = bot

    # 非 6 位码
    non6 = t.execute("SELECT COUNT(*) FROM trades WHERE symbol NOT GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'").fetchone()[0]
    res["non6_symbol_rows"] = non6

    # 集中度 Top1 / Top5
    top = t.execute("SELECT symbol, COUNT(*) n FROM trades GROUP BY symbol ORDER BY n DESC LIMIT 5").fetchall()
    total = c1
    top1_ratio = top[0]["n"]/total*100 if top else 0
    top5_ratio = sum(r["n"] for r in top)/total*100 if top else 0
    res["top1"] = {"symbol": top[0]["symbol"], "n": top[0]["n"], "ratio_pct": round(top1_ratio,1)} if top else None
    res["top5_ratio_pct"] = round(top5_ratio,1)
    res["top5_list"] = [{"symbol": r["symbol"], "n": r["n"]} for r in top]

    # join 健康度 trades.author -> players.name
    jh = t.execute("""
        SELECT COUNT(*) total,
               SUM(CASE WHEN p.name IS NOT NULL THEN 1 ELSE 0 END) matched
        FROM trades tr LEFT JOIN players p ON tr.author=p.name
    """).fetchone()
    res["join_health"] = {"total": jh["total"], "matched": jh["matched"], "pct": round(jh["matched"]/jh["total"]*100,1) if jh["total"] else 0}

    # Tier A 入围选手
    tierA = t.execute("""
        SELECT COUNT(DISTINCT tr.author) authors, COUNT(*) rows
        FROM trades tr JOIN players p ON tr.author=p.name
        WHERE p.tier='A'
    """).fetchone()
    res["tierA"] = {"authors": tierA["authors"], "rows": tierA["rows"]}

    c.close()

    # 备份存在性 + 大小
    res["backup_exists"] = os.path.exists(BAK)
    if res["backup_exists"]:
        res["backup_size_mb"] = round(os.path.getsize(BAK)/1024/1024, 2)

    # WAL 检查点（安全写操作，将已提交 WAL 帧并入主库文件）
    try:
        w = sqlite3.connect(DB)
        wc = w.cursor()
        wc.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        row = wc.fetchone()
        w.close()
        # row = (busy, log, checkpointed)
        res["wal_checkpoint"] = {"busy": row[0], "log_remaining": row[1], "checkpointed": row[2]}
    except Exception as e:
        res["wal_checkpoint"] = {"error": str(e)}

    print(json.dumps(res, ensure_ascii=False, indent=2))

    # 通过判据
    ok = (res["trades_rows"]==941 and res["symbol_000688_rows"]==0
          and res["bot_residue"]==0 and res["non6_symbol_rows"]==0
          and res["join_health"]["pct"]==100.0 and res["backup_exists"])
    print("\nFINAL_QA_PASS:", ok)
    return ok

if __name__ == "__main__":
    main()
