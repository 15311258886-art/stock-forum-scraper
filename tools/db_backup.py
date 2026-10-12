# -*- coding: utf-8 -*-
"""本地数据库备份与完整性校验（规格书 D1，本地部分）。

背景：`viewer/backups/` 里堆的是历次改造前的**手工临时快照**（无保留策略、体积只增不减），
既没有"每日一份"，也没有启动时的完整性校验。本工具补上这两件事（异地副本待定）。

设计要点：
  · 用 sqlite3 的**在线备份 API**（`src.backup(dst)`）而不是文件复制 —— 服务在跑、有 WAL 待落盘时
    文件拷贝会拿到不一致的副本，在线备份才是安全的。
  · 保留策略：每日 1 份留 `--keep-daily`（默认 7）天；每周（周一）1 份留 `--keep-weekly`（默认 4）周。
  · 只清理**本工具自己产生的**文件（`app.db.daily-*` / `app.db.weekly-*`），
    不动历史手工快照 `app.db.pre-*`（那些是改造前的还原点，需人工确认后再清）。
  · `--check` 只做 `PRAGMA integrity_check`，不写任何文件。

用法：
    .venv\\Scripts\\python.exe tools/db_backup.py            # 每日备份（幂等：当天已备则跳过）
    .venv\\Scripts\\python.exe tools/db_backup.py --check    # 只校验完整性
    .venv\\Scripts\\python.exe tools/db_backup.py --force    # 忽略"当天已备"限制
退出码：0=成功（校验模式下=库完好）；1=失败/库损坏。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "viewer", "app.db")
BACKUP_DIR = os.path.join(ROOT, "viewer", "backups")


def integrity_check(db_path: str = DB_PATH) -> tuple:
    """返回 (ok, message)。只读打开，不产生副作用。"""
    if not os.path.exists(db_path):
        return False, "主库不存在：%s" % db_path
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True, timeout=10)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            res = (row[0] if row else "unknown")
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        return False, "校验异常：%s: %s" % (type(e).__name__, e)
    return (res == "ok"), ("完好" if res == "ok" else "报告：%s" % res)


def _online_backup(db_path: str, dst: str) -> None:
    src = sqlite3.connect(db_path, timeout=30)
    try:
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def _prune(prefix: str, keep: int) -> list:
    """按文件名里的日期倒序保留最近 keep 个，返回被删除的文件名列表。"""
    if not os.path.isdir(BACKUP_DIR):
        return []
    files = sorted((f for f in os.listdir(BACKUP_DIR) if f.startswith(prefix)), reverse=True)
    removed = []
    for f in files[keep:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, f))
            removed.append(f)
        except OSError:
            pass
    return removed


def daily_backup(db_path: str = DB_PATH, keep_daily: int = 7, keep_weekly: int = 4,
                 force: bool = False) -> tuple:
    """返回 (ok, message, path)。"""
    ok, msg = integrity_check(db_path)
    if not ok:
        return False, "备份前完整性校验未通过：%s" % msg, ""

    os.makedirs(BACKUP_DIR, exist_ok=True)
    today = _dt.date.today()
    stamp = today.strftime("%Y%m%d")

    is_monday = today.weekday() == 0
    targets = [("app.db.daily-%s" % stamp, keep_daily)]
    if is_monday:
        targets.append(("app.db.weekly-%s" % stamp, keep_weekly))

    made = []
    for name, keep in targets:
        dst = os.path.join(BACKUP_DIR, name)
        if os.path.exists(dst) and not force:
            continue
        _online_backup(db_path, dst)
        made.append(name)
        _prune(name.split("%s" % stamp)[0], keep)

    if not made:
        return True, "今日已备份，跳过（--force 可强制重备）", ""
    size = os.path.getsize(os.path.join(BACKUP_DIR, made[0])) / 1024 / 1024
    return True, "已备份：%s（%.1f MB）；保留策略 daily=%d / weekly=%d" % (
        "、".join(made), size, keep_daily, keep_weekly), os.path.join(BACKUP_DIR, made[0])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地数据库备份与完整性校验")
    ap.add_argument("--check", action="store_true", help="只做完整性校验，不备份")
    ap.add_argument("--force", action="store_true", help="忽略当天已备份的限制")
    ap.add_argument("--keep-daily", type=int, default=7)
    ap.add_argument("--keep-weekly", type=int, default=4)
    args = ap.parse_args(argv)

    if args.check:
        ok, msg = integrity_check()
        print("[db_backup] 完整性校验：%s —— %s" % ("PASS" if ok else "FAIL", msg))
        return 0 if ok else 1

    ok, msg, path = daily_backup(keep_daily=args.keep_daily, keep_weekly=args.keep_weekly,
                                 force=args.force)
    print("[db_backup] %s —— %s" % ("PASS" if ok else "FAIL", msg))
    if path:
        print("[db_backup] 备份文件：%s" % path)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
