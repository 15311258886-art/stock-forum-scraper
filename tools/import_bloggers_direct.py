# -*- coding: utf-8 -*-
"""直接把「博主荐股」源头 Excel 灌进 viewer/app.db（幂等，可反复执行）。

【为什么会有这个脚本（2026-10-09 重构）】
原先 tools/import_blogger_contest.py 读的是**另一个 App（博主数据查看台）的静态产物**
`...\\博主数据查看台\\public\\data\\bloggers.json` —— 那只是 sync_bloggers.py 把本机
Excel `独股九剑推荐股票追踪.xlsx` 二次导出的中间文件。链路一长就多两个故障点：
原 App 不跑 → bloggers.json 不刷新 → 查看器博主数据永远停在旧值。
本脚本**直接把源头 Excel 读进来**，砍掉对原 App 的依赖（不再需要 bloggers.json）。

【数据源】
  · 博主荐股 ← 本机 Excel `F:\\AI Agent部署知识库\\独股九剑推荐股票追踪.xlsx`
               用 openpyxl 直读，复刻原 App tools/sync_bloggers.py 的三 sheet 抽取逻辑
               （含「博主」列纵向合并需**向下填充**）。
  · 参赛股   ← 静态文件 contest.json（磁盘上就有，**读它不需要原 App 运行**）。

【落库表（与 import_blogger_contest.py 同一批，零耦合既有业务表）】
  blogger_reco / blogger_overlap / blogger_stats
  contest_round / contest_participant / contest_track

【幂等键】（沿用 import_blogger_contest.py，保证两脚本可共存、可反复跑）
  blogger_reco      UNIQUE(blogger, rec_date, stock_code)
  blogger_overlap   UNIQUE(stock_code)
  blogger_stats     UNIQUE(blogger)
  contest_round     UNIQUE(video_id)
  contest_participant UNIQUE(video_id, participant)
  contest_track     UNIQUE(video_id, participant, trade_date)

【口径诚实】
  · src_updated 取**源文件最后修改时间**（Excel / contest.json 的 mtime），
    而不是「本次导入时刻」——它表达「源数据本身更新于何时」，且反复跑不会漂移。
  · 数值一律 round 到 3 位（与原 sync_bloggers.py 的 to_num 一致），确保落库值与旧链路逐位相同。
  · 逐日追踪 change_pct 缺失一律 NULL，**绝不写 0**（沿用 contest_track 硬口径）。
  · 只 UPSERT、不 DELETE：与既有脚本行为一致，不误删任何既有行（不破坏）。

【用法】
  python tools/import_bloggers_direct.py                # 全量灌数（Excel + contest.json）
  python tools/import_bloggers_direct.py --no-contest   # 只灌 Excel（博主荐股三表）
  python tools/import_bloggers_direct.py --quiet        # 静默（供调度器调用，只回一行汇总）
  # 源路径可用环境变量覆盖：BLOGGER_XLSX / BLOGGER_CONTEST_JSON
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys

try:
    import openpyxl
except ImportError:  # pragma: no cover —— 运行环境缺依赖时给出可执行的修复提示
    openpyxl = None

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
DB = os.path.join(PROJ, "viewer", "app.db")

# ---- 源路径：**多候选 + 取最新**（2026-10-12 加） ----
# 事故背景：2026-10-11 另一个 agent 的工作目录整体搬进 `豆包每日抓取\`，Excel 从知识库根目录
# 挪到 `知识库根\豆包每日抓取\`；而这里写死旧路径 → 每日任务 rc=2（源 Excel 缺失）
# → **连参赛股也一起断更**（viewer/logs/blogger_daily.log 里 10-11 08:30 连续 3 次 rc=2）。
# 教训：源目录会搬家，写死单一路径 = 下次再搬又静默断更。改为候选列表 + 取 mtime 最新者。
_XLSX_CANDIDATES = [
    r"F:\AI Agent部署知识库\豆包每日抓取\独股九剑推荐股票追踪.xlsx",   # 2026-10-11 起的新位置
    r"F:\AI Agent部署知识库\独股九剑推荐股票追踪.xlsx",               # 旧位置（保留兼容）
]
_CONTEST_CANDIDATES = [
    r"F:\AI Agent部署知识库\豆包每日抓取\data\contest.json",                      # 2026-10-11 起
    os.path.join(r"F:\AI Agent部署知识库", "博主数据查看台", "public", "data", "contest.json"),
]


def pick_newest(cands: list, env_key: str) -> str:
    """从候选路径里挑「存在且最新」的一个；都不存在则返回首个（调用方会明确报缺失）。

    环境变量优先，但**必须真实存在**才算数 —— 否则设错一个路径反而更难排查。
    """
    env = os.environ.get(env_key)
    if env and os.path.exists(env):
        return env
    exist = [p for p in cands if os.path.exists(p)]
    return max(exist, key=os.path.getmtime) if exist else cands[0]


# 博主荐股唯一真源：本机 Excel（自动定位，勿再写死单一路径）。
XLSX = pick_newest(_XLSX_CANDIDATES, "BLOGGER_XLSX")
# 参赛股：静态 contest.json（文件在磁盘上，无需原 App 运行）。
CONTEST_JSON = pick_newest(_CONTEST_CANDIDATES, "BLOGGER_CONTEST_JSON")

# 三个 sheet 名（与源表一致）。
SHEET_RECO = "独股九剑推荐股票追踪"
SHEET_OVERLAP = "重复推荐股票"
SHEET_STATS = "博主效果对比"

# 与 import_blogger_contest.py 完全一致的 DDL（两脚本可共存；CREATE IF NOT EXISTS 幂等）。
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


# ---------------------------------------------------------------- 基础转换
def _s(v) -> str:
    """任意值 → 去空白字符串（None → ""）。"""
    if v is None:
        return ""
    return str(v).strip()


def _num(v):
    """任意值 → round(float, 3)，失败/空返回 None（对齐 sync_bloggers.to_num）。"""
    if v is None or v == "":
        return None
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


def _date(v):
    """任意值 → 'YYYY-MM-DD' 字符串；datetime/date 走格式化，其余取 str().strip()。"""
    if v is None:
        return None
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, datetime.date):
        return v.strftime("%Y-%m-%d")
    s = _s(v)
    return s if s else None


def _mtime_str(path: str) -> str:
    """源文件最后修改时间 'YYYY-MM-DD HH:MM'（用于 src_updated / updated_at）。"""
    try:
        return datetime.datetime.fromtimestamp(
            os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- Excel 抽取
def extract_bloggers_from_xlsx(xlsx_path: str) -> tuple[list, list, list]:
    """直读 Excel，返回 (荐股明细, 重复推荐, 博主效果对比) 三份记录。

    复刻原 App tools/sync_bloggers.py 的抽取逻辑：
      · sheet1「博主」列**纵向合并**，须向下填充（合并单元格仅首行有值）；
      · sheet1 以「股票名称」非空为有效行判据；
      · sheet2/3 以「股票代码 / 博主」非空为有效行判据。
    """
    if openpyxl is None:
        raise RuntimeError("缺少 openpyxl 依赖：请先 pip install openpyxl（工程 .venv 已自带）")
    if not os.path.exists(xlsx_path):
        raise FileNotFoundError("源 Excel 缺失：%s" % xlsx_path)

    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    try:
        # ---- 1) 荐股明细（博主列向下填充）----
        ws = wb[SHEET_RECO]
        stocks: list[dict] = []
        current_blogger: str | None = None
        for r in ws.iter_rows(min_row=2, values_only=True):
            blogger = _s(r[0]) if len(r) > 0 else ""
            if blogger:
                current_blogger = blogger
            if not current_blogger:
                continue
            name = _s(r[3]) if len(r) > 3 else ""
            if not name:
                continue
            stocks.append({
                "博主": current_blogger,
                "序号": r[1] if len(r) > 1 else None,
                "推荐日期": _date(r[2] if len(r) > 2 else None),
                "股票名称": name,
                "股票代码": _s(r[4]) if len(r) > 4 else "",
                "推荐时价格": _num(r[5] if len(r) > 5 else None),
                "一周最高价": _num(r[6] if len(r) > 6 else None),
                "最新收盘价": _num(r[7] if len(r) > 7 else None),
                "历史最高价": _num(r[8] if len(r) > 8 else None),
                "距高点跌幅": _num(r[9] if len(r) > 9 else None),
                "推荐以来涨跌幅": _num(r[10] if len(r) > 10 else None),
                "来源视频": _s(r[11]) if len(r) > 11 else "",
                "简单分析": _s(r[12]) if len(r) > 12 else "",
            })

        # ---- 2) 重复推荐股票 ----
        ws2 = wb[SHEET_OVERLAP]
        overlaps: list[dict] = []
        for r in ws2.iter_rows(min_row=2, values_only=True):
            code = _s(r[0]) if len(r) > 0 else ""
            if not code:
                continue
            overlaps.append({
                "股票代码": code,
                "股票名称": _s(r[1]) if len(r) > 1 else "",
                "推荐博主数": r[2] if len(r) > 2 else None,
                "推荐博主": _s(r[3]) if len(r) > 3 else "",
                "推荐时价格": _num(r[4] if len(r) > 4 else None),
                "最新收盘价": _num(r[5] if len(r) > 5 else None),
                "推荐以来涨跌幅": _num(r[6] if len(r) > 6 else None),
                "简单分析": _s(r[7]) if len(r) > 7 else "",
            })

        # ---- 3) 博主效果对比 ----
        ws3 = wb[SHEET_STATS]
        stats: list[dict] = []
        for r in ws3.iter_rows(min_row=2, values_only=True):
            blogger = _s(r[0]) if len(r) > 0 else ""
            if not blogger:
                continue
            stats.append({
                "博主": blogger,
                "推荐股票数": r[1] if len(r) > 1 else None,
                "平均涨跌幅": _num(r[2] if len(r) > 2 else None),
                "平均一周涨幅": _num(r[3] if len(r) > 3 else None),
                "上涨比例": _num(r[4] if len(r) > 4 else None),
                "最高涨幅": _num(r[5] if len(r) > 5 else None),
                "最低涨幅": _num(r[6] if len(r) > 6 else None),
            })
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass
    return stocks, overlaps, stats


# ---------------------------------------------------------------- 落库（UPSERT）
def upsert_bloggers(con: sqlite3.Connection, stocks: list, overlaps: list,
                    stats: list, src_updated: str) -> tuple[int, int, int]:
    """把三份记录 UPSERT 进 blogger_reco / blogger_overlap / blogger_stats。"""
    n_reco = n_ovl = n_sta = 0

    for r in stocks:
        con.execute(
            "INSERT INTO blogger_reco (blogger, seq, rec_date, stock_name, stock_code, price_at_rec,"
            " high_1w, close_latest, high_hist, drop_from_high, ret_since, video_title, note, src_updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(blogger, rec_date, stock_code) DO UPDATE SET"
            " seq=excluded.seq, stock_name=excluded.stock_name, price_at_rec=excluded.price_at_rec,"
            " high_1w=excluded.high_1w, close_latest=excluded.close_latest, high_hist=excluded.high_hist,"
            " drop_from_high=excluded.drop_from_high, ret_since=excluded.ret_since,"
            " video_title=excluded.video_title, note=excluded.note, src_updated=excluded.src_updated",
            (_s(r.get("博主")), _num(r.get("序号")), _s(r.get("推荐日期")), _s(r.get("股票名称")),
             _s(r.get("股票代码")), _num(r.get("推荐时价格")), _num(r.get("一周最高价")),
             _num(r.get("最新收盘价")), _num(r.get("历史最高价")), _num(r.get("距高点跌幅")),
             _num(r.get("推荐以来涨跌幅")), _s(r.get("来源视频")), _s(r.get("简单分析")), src_updated))
        n_reco += 1

    for r in overlaps:
        con.execute(
            "INSERT INTO blogger_overlap (stock_code, stock_name, n_bloggers, bloggers, price_at_rec,"
            " close_latest, ret_since, note, src_updated)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(stock_code) DO UPDATE SET"
            " stock_name=excluded.stock_name, n_bloggers=excluded.n_bloggers, bloggers=excluded.bloggers,"
            " price_at_rec=excluded.price_at_rec, close_latest=excluded.close_latest,"
            " ret_since=excluded.ret_since, note=excluded.note, src_updated=excluded.src_updated",
            (_s(r.get("股票代码")), _s(r.get("股票名称")), _num(r.get("推荐博主数")), _s(r.get("推荐博主")),
             _num(r.get("推荐时价格")), _num(r.get("最新收盘价")), _num(r.get("推荐以来涨跌幅")),
             _s(r.get("简单分析")), src_updated))
        n_ovl += 1

    for r in stats:
        con.execute(
            "INSERT INTO blogger_stats (blogger, n_stock, avg_ret, avg_ret_1w, up_ratio, max_ret, min_ret, src_updated)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(blogger) DO UPDATE SET"
            " n_stock=excluded.n_stock, avg_ret=excluded.avg_ret, avg_ret_1w=excluded.avg_ret_1w,"
            " up_ratio=excluded.up_ratio, max_ret=excluded.max_ret, min_ret=excluded.min_ret,"
            " src_updated=excluded.src_updated",
            (_s(r.get("博主")), _num(r.get("推荐股票数")), _num(r.get("平均涨跌幅")),
             _num(r.get("平均一周涨幅")), _num(r.get("上涨比例")), _num(r.get("最高涨幅")),
             _num(r.get("最低涨幅")), src_updated))
        n_sta += 1

    return n_reco, n_ovl, n_sta


def upsert_contest(con: sqlite3.Connection, cj: dict, c_updated: str) -> tuple[int, int, int]:
    """把 contest.json（静态文件）UPSERT 进 contest_round / contest_participant / contest_track。"""
    n_rnd = n_par = n_trk = 0
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

        for i, s in enumerate(v.get("stocks") or []):
            con.execute(
                "INSERT INTO contest_participant (video_id, rank_no, participant, stock, code, week_gain, points)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(video_id, participant) DO UPDATE SET"
                " rank_no=excluded.rank_no, stock=excluded.stock, code=excluded.code,"
                " week_gain=excluded.week_gain, points=excluded.points",
                (vid, i + 1, _s(s.get("participant")), _s(s.get("stock")), _s(s.get("code")),
                 _num(s.get("weekGain")), _num(s.get("points"))))
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
                     _num(t.get("close")), _num(t.get("changePct"))))
                n_trk += 1
    return n_rnd, n_par, n_trk


# ---------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="直接把博主荐股源头 Excel 灌进 viewer/app.db（幂等）")
    ap.add_argument("--xlsx", default=XLSX, help="博主荐股 Excel 路径（默认源表）")
    ap.add_argument("--contest-json", default=CONTEST_JSON, help="参赛股 contest.json 路径")
    ap.add_argument("--db", default=DB, help="目标 SQLite 库路径")
    ap.add_argument("--no-contest", action="store_true", help="只灌 Excel（博主荐股三表）")
    ap.add_argument("--quiet", action="store_true", help="只输出一行汇总（供调度器）")
    args = ap.parse_args(argv)

    # 两个数据源**解耦**（2026-10-12）：源 Excel 缺失只跳过「博主荐股三表」，**参赛股照灌**。
    # 事故背景：Excel 被挪走后旧实现直接 return 2 → 参赛股也一起断更，界面上看着像"两边都旧"。
    bloggers_ok = os.path.exists(args.xlsx)
    if not bloggers_ok:
        print("[import_bloggers_direct] ⚠ 源 Excel 缺失：%s" % args.xlsx, file=sys.stderr)
        print("[import_bloggers_direct]   候选：%s" % " | ".join(_XLSX_CANDIDATES), file=sys.stderr)
        print("[import_bloggers_direct]   → 跳过博主荐股三表，继续灌参赛股（两者已解耦）", file=sys.stderr)

    # 1) 直读源头 Excel（不再经过原 App 的 bloggers.json）
    if bloggers_ok:
        stocks, overlaps, stats = extract_bloggers_from_xlsx(args.xlsx)
        src_updated = _mtime_str(args.xlsx)
    else:
        stocks, overlaps, stats, src_updated = [], [], [], ""

    con = sqlite3.connect(args.db, timeout=30)
    con.row_factory = sqlite3.Row
    n_reco = n_ovl = n_sta = 0
    n_rnd = n_par = n_trk = 0
    contest_note = "skipped"
    try:
        con.executescript(DDL)
        if bloggers_ok:
            n_reco, n_ovl, n_sta = upsert_bloggers(con, stocks, overlaps, stats, src_updated)

        if not args.no_contest:
            if os.path.exists(args.contest_json):
                with open(args.contest_json, encoding="utf-8") as f:
                    cj = json.load(f)
                n_rnd, n_par, n_trk = upsert_contest(con, cj, _mtime_str(args.contest_json))
                contest_note = "ok"
            else:
                contest_note = "missing:%s" % args.contest_json

        con.commit()

        # 2026-10-09 口径自愈（根治「汇总表不跟着 reco 重算」）：
        # 源头 Excel 只描述得了它自己有的行；抖音自动提取等渠道直接新增 blogger_reco 行后，
        # 汇总口径（blogger_stats / blogger_overlap）必须从 **blogger_reco 真源** 重算，
        # 否则 Excel 一灌就把汇总表退回旧值、永远滞后。best-effort：失败只记，不影响导入结果。
        # 实现已收敛到顶层模块 blogger_rebuild（与 server 读取时自愈、CLI 脚本共用一份口径）。
        try:
            _proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _proj not in sys.path:
                sys.path.insert(0, _proj)
            import blogger_rebuild as _rb
            _rb.rebuild_stats(con)
            _rb.rebuild_overlap(con)
        except Exception as _e:  # noqa: BLE001
            print("[import_bloggers_direct] 汇总重算失败（已忽略，不影响导入）：%s" % _e,
                  file=sys.stderr)

        def cnt(tbl: str) -> int:
            try:
                return con.execute("SELECT COUNT(*) FROM %s" % tbl).fetchone()[0]
            except sqlite3.Error:
                return -1

        bloggers = ", ".join(
            r[0] for r in con.execute(
                "SELECT blogger FROM blogger_reco GROUP BY blogger ORDER BY blogger"))
        rounds = con.execute(
            "SELECT COUNT(DISTINCT video_id) FROM contest_participant").fetchone()[0]

        if args.quiet:
            # tag 会进 viewer/logs/blogger_daily.log 的每行摘要 —— 让"博主源缺失、参赛股照灌"
            # 这件事在日志里一眼可见，而不是静默降级。
            tag = "ok" if bloggers_ok else "PARTIAL(bloggers=MISSING)"
            print("import_bloggers_direct %s reco=%d overlap=%d stats=%d "
                  "round=%d part=%d track=%d contest=%s src=%s"
                  % (tag, cnt("blogger_reco"), cnt("blogger_overlap"), cnt("blogger_stats"),
                     cnt("contest_round"), cnt("contest_participant"), cnt("contest_track"),
                     contest_note, src_updated))
        else:
            print("直接导入完成（源头 Excel）→ %s" % args.db)
            print("  源 Excel ：%s（mtime %s）" % (args.xlsx, src_updated))
            print("  blogger_reco        %d 行（源 %d）" % (cnt("blogger_reco"), n_reco))
            print("  blogger_overlap     %d 行（源 %d）" % (cnt("blogger_overlap"), n_ovl))
            print("  blogger_stats       %d 行（源 %d）" % (cnt("blogger_stats"), n_sta))
            print("  contest_round       %d 行（源 %d）" % (cnt("contest_round"), n_rnd))
            print("  contest_participant %d 行（源 %d）" % (cnt("contest_participant"), n_par))
            print("  contest_track       %d 行（源 %d）" % (cnt("contest_track"), n_trk))
            print("  contest.json        %s" % contest_note)
            print("  博主名单：%s" % bloggers)
            print("  参赛期数：%d" % rounds)
    finally:
        con.close()
    # 只有「两个源都拿不到」才算真失败；Excel 缺失但参赛股灌成功 → 仍算成功
    # （部分降级已由上面的 PARTIAL 标记写进日志，不静默）。
    if not bloggers_ok and contest_note != "ok":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
