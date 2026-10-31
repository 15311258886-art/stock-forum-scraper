# -*- coding: utf-8 -*-
"""导入「博主荐股」与「当红参赛股」两块业务数据到 viewer/app.db（幂等，可重复执行）。

数据源（**不重新抓取**，直接复用王总现有查看台的存量产物）：
  · bloggers.json  —— 5 位荐股博主明细 237 条 + 重复推荐 15 只 + 博主效果对比 6 行
                      源头为 F:\\AI Agent部署知识库\\独股九剑推荐股票追踪.xlsx（tools/sync_bloggers.py 导出）
  · contest.json   —— BRTV天下财经「当红不让」参赛股 2 期，含每人逐日追踪

落库表（全部新建，与既有表零耦合）：
  blogger_reco / blogger_overlap / blogger_stats
  contest_round / contest_participant / contest_track

幂等键：
  blogger_reco  UNIQUE(blogger, rec_date, stock_code)   —— 同一博主同一天推同一只票，重复执行只覆盖
  blogger_overlap UNIQUE(stock_code)                    —— 以股票为键（每只票只可能出现一次）
  blogger_stats UNIQUE(blogger)
  contest_round UNIQUE(video_id)
  contest_participant UNIQUE(video_id, participant)     —— 参赛名在同一期内唯一（含「空仓」）
  contest_track UNIQUE(video_id, participant, trade_date)

逐日追踪走**规范化存储**（一行一天）而非 JSON blob，理由是：不同期数的日期轴可能不同，
前端需要按实际日期轴 pivot 成色块网格；规范化后日期轴直接 SELECT DISTINCT 得到，
不用解析 JSON、不用假设所有人的日期区间一致。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
DB = os.environ.get("VIEWER_DB") or os.path.join(PROJ, "viewer", "app.db")

SRC_DIR = r"F:\AI Agent部署知识库\博主数据查看台\public\data"

DDL = """
CREATE TABLE IF NOT EXISTS blogger_reco (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  blogger TEXT NOT NULL, seq INTEGER, rec_date TEXT, stock_name TEXT, stock_code TEXT,
  price_at_rec REAL, high_1w REAL, close_latest REAL, high_hist REAL,
  drop_from_high REAL, ret_since REAL, video_title TEXT, note TEXT,
  src_updated TEXT, UNIQUE(blogger, rec_date, stock_code)
);
CREATE TABLE IF NOT EXISTS blogger_overlap (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_code TEXT NOT NULL, stock_name TEXT, n_bloggers INTEGER, bloggers TEXT,
  price_at_rec REAL, close_latest REAL, ret_since REAL, note TEXT,
  src_updated TEXT, UNIQUE(stock_code)
);
CREATE TABLE IF NOT EXISTS blogger_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  blogger TEXT NOT NULL, n_stock INTEGER, avg_ret REAL, avg_ret_1w REAL,
  up_ratio REAL, max_ret REAL, min_ret REAL, src_updated TEXT, UNIQUE(blogger)
);
CREATE TABLE IF NOT EXISTS contest_round (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL, title TEXT, publish_date TEXT, author TEXT,
  video_url TEXT, updated_at TEXT, UNIQUE(video_id)
);
CREATE TABLE IF NOT EXISTS contest_participant (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL, rank_no INTEGER, participant TEXT, stock TEXT, code TEXT,
  week_gain REAL, points REAL, UNIQUE(video_id, participant)
);
CREATE TABLE IF NOT EXISTS contest_track (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT, participant TEXT, code TEXT, trade_date TEXT,
  close REAL, change_pct REAL,
  UNIQUE(video_id, participant, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_br_blogger ON blogger_reco(blogger);
CREATE INDEX IF NOT EXISTS idx_br_code ON blogger_reco(stock_code);
CREATE INDEX IF NOT EXISTS idx_ct_video ON contest_track(video_id);
"""


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _s(v):
    if v is None:
        return ""
    return str(v).strip()


def load(fn: str) -> dict:
    p = os.path.join(SRC_DIR, fn)
    if not os.path.exists(p):
        raise SystemExit("数据源缺失：%s" % p)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    bj = load("bloggers.json")
    cj = load("contest.json")
    updated = bj.get("updatedAt") or ""
    c_updated = cj.get("updatedAt") or ""

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(DDL)

    n_reco = n_ovl = n_sta = n_rnd = n_par = n_trk = 0

    # ---------- 1) 博主荐股明细 ----------
    for r in bj.get("stocks") or []:
        con.execute(
            "INSERT INTO blogger_reco (blogger, seq, rec_date, stock_name, stock_code, price_at_rec,"
            " high_1w, close_latest, high_hist, drop_from_high, ret_since, video_title, note, src_updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(blogger, rec_date, stock_code) DO UPDATE SET"
            " seq=excluded.seq, stock_name=excluded.stock_name, price_at_rec=excluded.price_at_rec,"
            " high_1w=excluded.high_1w, close_latest=excluded.close_latest, high_hist=excluded.high_hist,"
            " drop_from_high=excluded.drop_from_high, ret_since=excluded.ret_since,"
            " video_title=excluded.video_title, note=excluded.note, src_updated=excluded.src_updated",
            (_s(r.get("博主")), _f(r.get("序号")), _s(r.get("推荐日期")), _s(r.get("股票名称")),
             _s(r.get("股票代码")), _f(r.get("推荐时价格")), _f(r.get("一周最高价")), _f(r.get("最新收盘价")),
             _f(r.get("历史最高价")), _f(r.get("距高点跌幅")), _f(r.get("推荐以来涨跌幅")),
             _s(r.get("来源视频")), _s(r.get("简单分析")), updated))
        n_reco += 1

    # ---------- 2) 重复推荐（多博主同荐一只）----------
    for r in bj.get("overlaps") or []:
        con.execute(
            "INSERT INTO blogger_overlap (stock_code, stock_name, n_bloggers, bloggers, price_at_rec,"
            " close_latest, ret_since, note, src_updated)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(stock_code) DO UPDATE SET"
            " stock_name=excluded.stock_name, n_bloggers=excluded.n_bloggers, bloggers=excluded.bloggers,"
            " price_at_rec=excluded.price_at_rec, close_latest=excluded.close_latest,"
            " ret_since=excluded.ret_since, note=excluded.note, src_updated=excluded.src_updated",
            (_s(r.get("股票代码")), _s(r.get("股票名称")), _f(r.get("推荐博主数")), _s(r.get("推荐博主")),
             _f(r.get("推荐时价格")), _f(r.get("最新收盘价")), _f(r.get("推荐以来涨跌幅")),
             _s(r.get("简单分析")), updated))
        n_ovl += 1

    # ---------- 3) 博主效果对比 ----------
    for r in bj.get("stats") or []:
        con.execute(
            "INSERT INTO blogger_stats (blogger, n_stock, avg_ret, avg_ret_1w, up_ratio, max_ret, min_ret, src_updated)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(blogger) DO UPDATE SET"
            " n_stock=excluded.n_stock, avg_ret=excluded.avg_ret, avg_ret_1w=excluded.avg_ret_1w,"
            " up_ratio=excluded.up_ratio, max_ret=excluded.max_ret, min_ret=excluded.min_ret,"
            " src_updated=excluded.src_updated",
            (_s(r.get("博主")), _f(r.get("推荐股票数")), _f(r.get("平均涨跌幅")), _f(r.get("平均一周涨幅")),
             _f(r.get("上涨比例")), _f(r.get("最高涨幅")), _f(r.get("最低涨幅")), updated))
        n_sta += 1

    # ---------- 4~6) 当红参赛股 ----------
    for v in cj.get("videos") or []:
        vid = _s(v.get("videoId"))
        if not vid:
            continue
        con.execute(
            "INSERT INTO contest_round (video_id, title, publish_date, author, video_url, updated_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(video_id) DO UPDATE SET"
            " title=excluded.title, publish_date=excluded.publish_date, author=excluded.author,"
            " video_url=excluded.video_url, updated_at=excluded.updated_at",
            (vid, _s(v.get("title")), _s(v.get("publishDate")), _s(v.get("author")),
             _s(v.get("videoUrl")), c_updated))
        n_rnd += 1

        # 参赛者先按原始顺序落位（rank 用出现序；「空仓」这类也如实保留）
        ranked = v.get("stocks") or []
        for i, s in enumerate(ranked):
            con.execute(
                "INSERT INTO contest_participant (video_id, rank_no, participant, stock, code, week_gain, points)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(video_id, participant) DO UPDATE SET"
                " rank_no=excluded.rank_no, stock=excluded.stock, code=excluded.code,"
                " week_gain=excluded.week_gain, points=excluded.points",
                (vid, i + 1, _s(s.get("participant")), _s(s.get("stock")), _s(s.get("code")),
                 _f(s.get("weekGain")), _f(s.get("points"))))
            n_par += 1

            for t in (s.get("tracking") or []):
                td = _s(t.get("date"))
                if not td:
                    continue
                con.execute(
                    "INSERT INTO contest_track (video_id, participant, code, trade_date, close, change_pct)"
                    " VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(video_id, participant, trade_date) DO UPDATE SET"
                    " close=excluded.close, change_pct=excluded.change_pct",
                    (vid, _s(s.get("participant")), _s(s.get("code")), td,
                     _f(t.get("close")), _f(t.get("changePct"))))
                n_trk += 1

    con.commit()

    # ---------- 校验 ----------
    def cnt(t):
        try:
            return con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        except sqlite3.Error:
            return -1

    print("导入完成 → %s" % DB)
    print("  blogger_reco        %d 行（源 %d）" % (cnt("blogger_reco"), n_reco))
    print("  blogger_overlap     %d 行（源 %d）" % (cnt("blogger_overlap"), n_ovl))
    print("  blogger_stats       %d 行（源 %d）" % (cnt("blogger_stats"), n_sta))
    print("  contest_round       %d 行（源 %d）" % (cnt("contest_round"), n_rnd))
    print("  contest_participant %d 行（源 %d）" % (cnt("contest_participant"), n_par))
    print("  contest_track       %d 行（源 %d）" % (cnt("contest_track"), n_trk))
    print("  博主名单：%s" % ", ".join(
        r[0] for r in con.execute("SELECT blogger FROM blogger_reco GROUP BY blogger ORDER BY blogger")))
    print("  参赛期数：%d" % con.execute("SELECT COUNT(DISTINCT video_id) FROM contest_participant").fetchone()[0])
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
