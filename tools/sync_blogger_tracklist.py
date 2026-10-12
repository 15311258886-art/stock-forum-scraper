# -*- coding: utf-8 -*-
"""同步「博主跟踪名单」进 viewer/app.db 的 blogger_tracklist 表（幂等，可反复执行）。

【为什么要这张表】
「博主荐股」面板的博主筛选 chip 由 ``/api/blogger/facets`` 驱动，原来只列出
``blogger_reco`` 里**已有数据**的博主（5 位）。但每日自动化真正跟踪的博主有 21 位
（见 ``dy_targets.json``），用户要求面板把所有**在跟踪的博主**都显示出来、
暂时没数据的标注「待采集」。于是把跟踪名单单独落一张表，facets 取
「跟踪名单 ∪ reco 里有数据的博主」的并集。

【数据源】
抖音每日自动化的目标清单 ``dy_targets.json``（数组，每项：
``nickname / unique_id / sec_uid / stock_works / works / kind / contest``）。
同一昵称可能重复出现（例如「独股九剑」出现两次），按 **nickname 去重、取 works 较大者**。

【落库】
``blogger_tracklist(blogger TEXT PRIMARY KEY, kind TEXT, contest INTEGER DEFAULT 0,
works INTEGER, src_updated TEXT)`` —— 幂等 UPSERT，**只增改不删**。

用法：
    python tools/sync_blogger_tracklist.py                 # 用默认源路径
    python tools/sync_blogger_tracklist.py --src X.json    # 覆盖源路径
    python tools/sync_blogger_tracklist.py --db Y.db       # 覆盖目标库
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)

# 抖音每日自动化跟踪目标清单（只读源，本脚本绝不写它）。
DEFAULT_SRC = r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\dy_targets.json"
DEFAULT_DB = os.path.join(PROJ, "viewer", "app.db")

DDL = """
CREATE TABLE IF NOT EXISTS blogger_tracklist (
  blogger TEXT PRIMARY KEY,
  kind TEXT,
  contest INTEGER DEFAULT 0,
  works INTEGER,
  src_updated TEXT
);
"""


def _s(v) -> str:
    """宽松字符串化：None → ''，其余 strip。"""
    if v is None:
        return ""
    return str(v).strip()


def _i(v):
    """宽松整型：转不了回 None（**绝不把缺失当 0**，0 与「没有」是两回事）。"""
    try:
        if v is None or v == "":
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def load_targets(src: str) -> list[dict]:
    """读 dy_targets.json（数组）。文件缺失或非数组 → 抛错退出，不静默造假。"""
    if not os.path.exists(src):
        raise FileNotFoundError("跟踪源清单缺失：%s" % src)
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("dy_targets.json 顶层不是数组：%s" % type(data).__name__)
    return data


def dedupe(targets: list[dict]) -> list[dict]:
    """按 nickname 去重；同名取 works 较大者（works 缺失按 -1 处理，空名跳过）。

    返回按 blogger 名升序的列表，便于打印/断言。
    """
    best: dict[str, dict] = {}
    for it in targets:
        if not isinstance(it, dict):
            continue
        name = _s(it.get("nickname"))
        if not name:
            continue
        w = _i(it.get("works"))
        key = (w if w is not None else -1)
        cur = best.get(name)
        if cur is None:
            best[name] = it
            continue
        cur_w = _i(cur.get("works"))
        if key > (cur_w if cur_w is not None else -1):
            best[name] = it
    return [best[k] for k in sorted(best.keys())]


def sync(db_path: str, targets: list[dict], src_mtime: str) -> int:
    """幂等 UPSERT 进 blogger_tracklist，返回写入的行数。"""
    rows = dedupe(targets)
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.executescript(DDL)
        for it in rows:
            name = _s(it.get("nickname"))
            con.execute(
                "INSERT INTO blogger_tracklist (blogger, kind, contest, works, src_updated)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(blogger) DO UPDATE SET"
                " kind=excluded.kind, contest=excluded.contest,"
                " works=excluded.works, src_updated=excluded.src_updated",
                (name,
                 _s(it.get("kind")) or None,
                 1 if it.get("contest") else 0,
                 _i(it.get("works")),
                 src_mtime))
        con.commit()
        n = con.execute("SELECT COUNT(*) FROM blogger_tracklist").fetchone()[0]
    finally:
        con.close()
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同步博主跟踪名单进 blogger_tracklist（幂等）")
    ap.add_argument("--src", default=DEFAULT_SRC, help="dy_targets.json 源路径")
    ap.add_argument("--db", default=DEFAULT_DB, help="目标 SQLite 库（默认 viewer/app.db）")
    args = ap.parse_args(argv)

    targets = load_targets(args.src)
    try:
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(args.src)))
    except OSError:
        mtime = time.strftime("%Y-%m-%d %H:%M:%S")

    total = sync(args.db, targets, mtime)

    print("同步完成 → %s" % os.path.abspath(args.db))
    print("  源清单        ：%s（%d 项 / mtime %s）" % (args.src, len(targets), mtime))
    print("  去重后唯一博主：%d" % len(dedupe(targets)))
    print("  blogger_tracklist 现有 %d 行" % total)
    con = sqlite3.connect(args.db)
    try:
        names = [r[0] for r in con.execute(
            "SELECT blogger FROM blogger_tracklist ORDER BY blogger").fetchall()]
    finally:
        con.close()
    print("  名单：%s" % "、".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
