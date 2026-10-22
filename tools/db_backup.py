# -*- coding: utf-8 -*-
"""本地数据库备份与完整性校验 + 异地双副本（规格书 D1）。

背景：`viewer/backups/` 里堆的是历次改造前的**手工临时快照**（无保留策略、体积只增不减），
既没有"每日一份"，也没有启动时的完整性校验。本工具补上这三件事：
  ① 每日自动备份到本地 viewer/backups/（在线备份 API，WAL 安全）；
  ② PRAGMA integrity_check 完整性校验（--check）；
  ③ D1 异地双副本：除本地外，把同一份备份**镜像到异地目录**（网盘/外接盘同步目录，
     如 D:/stock-backups/），盘挂不全丢。异地目录可配置（命令行 --remote-dir
     或环境变量 DB_BACKUP_REMOTE_DIR），保留策略与本地一致（30 天日备份 + 每周全量）。

设计要点：
  · 用 sqlite3 的**在线备份 API**（`src.backup(dst)`）而不是文件复制 —— 服务在跑、有 WAL 待落盘时
    文件拷贝会拿到不一致的副本，在线备份才是安全的。
  · 保留策略：每日 1 份留 `--keep-daily`（默认 30）天；每周（周一）1 份留 `--keep-weekly`（默认 4）周。
  · 只清理**本工具自己产生的**文件（`app.db.daily-*` / `app.db.weekly-*`），
    不动历史手工快照 `app.db.pre-*`（那些是改造前的还原点，需人工确认后再清）。
  · 异地镜像用同样在线备份 API 直写异地库文件（不复制本地备份文件），一致性有保证；
    异地目录不可写时降级为"仅本地"并如实告警，不阻断主流程。
  · `--check` 只做 `PRAGMA integrity_check`，不写任何文件。

用法：
    .venv\\Scripts\\python.exe tools/db_backup.py            # 每日备份（幂等：当天已备则跳过）
    .venv\\Scripts\\python.exe tools/db_backup.py --check    # 只校验完整性
    .venv\\Scripts\\python.exe tools/db_backup.py --force    # 忽略"当天已备"限制
    .venv\\Scripts\\python.exe tools/db_backup.py --remote-dir "E:/backup/stock-backups"
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

# ---- D1 异地双副本（2026-10-13）：本地 viewer/backups + 异地目录各留一份 ----
# 异地目录 = 网盘 / 外接盘同步目录（如 D:/stock-backups/），盘挂不全丢。
# 配置优先级：命令行 --remote-dir > 环境变量 DB_BACKUP_REMOTE_DIR > 默认 D:/stock-backups。
DEFAULT_REMOTE_BACKUP_DIR = os.environ.get("DB_BACKUP_REMOTE_DIR", "D:/stock-backups")
# 镜像文件名统一前缀标记（BACKUP_MIRROR）：异地文件与本地备份一一对应，便于核对与恢复。
BACKUP_MIRROR = "backup_mirror"


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


def _prune(prefix: str, keep: int, d: str | None = None) -> list:
    """按文件名里的日期倒序保留最近 keep 个，返回被删除的文件名列表。

    d 默认为本地 BACKUP_DIR；异地镜像目录用同一套保留策略（--keep-daily / --keep-weekly）。
    目录不存在时直接返回 []（幂等）。
    """
    d = d or BACKUP_DIR
    if not os.path.isdir(d):
        return []
    files = sorted((f for f in os.listdir(d) if f.startswith(prefix)), reverse=True)
    removed = []
    for f in files[keep:]:
        try:
            os.remove(os.path.join(d, f))
            removed.append(f)
        except OSError:
            pass
    return removed


def _mirror_backup(db_path: str, name: str, remote_dir: str) -> str | None:
    """把本地备份 name 镜像到异地目录（D1 双副本）。

    直接用 sqlite3 在线备份 API 从主库写异地文件（不走「复制本地备份文件」），
    与本地备份同口径、无 WAL 不一致风险；返回异地文件路径，失败返回 None（不抛）。
    异地目录缺失时自动创建（网盘/外接盘未挂载会导致创建失败 → 调用方降级为仅本地）。
    """
    if not remote_dir:
        return None
    try:
        os.makedirs(remote_dir, exist_ok=True)
        dst = os.path.join(remote_dir, BACKUP_MIRROR + "-" + name)
        _online_backup(db_path, dst)
        return dst
    except Exception:  # noqa: BLE001 —— 异地不可用不影响本地备份，只记录降级
        return None


def daily_backup(db_path: str = DB_PATH, keep_daily: int = 30, keep_weekly: int = 4,
                 force: bool = False, remote_dir: str | None = None) -> tuple:
    """返回 (ok, message, path)。ok=False 仅当本地备份/校验本身失败；异地失败只降级不阻断。

    remote_dir：异地备份目录（D1 双副本）。传给 None 时用 DEFAULT_REMOTE_BACKUP_DIR；
    异地目录不可写时 message 里如实标注「异地副本降级」，绝不在日志里假装已经备份了两份。
    """
    if remote_dir is None:
        remote_dir = DEFAULT_REMOTE_BACKUP_DIR
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
    mirror_note = ""
    mirrored = []
    for name, keep in targets:
        dst = os.path.join(BACKUP_DIR, name)
        if os.path.exists(dst) and not force:
            continue
        _online_backup(db_path, dst)
        made.append(name)
        _prune(name.split("%s" % stamp)[0], keep)
        # D1 异地双副本：同一份备份镜像到异地目录（网盘/外接盘），保留策略与本地一致
        rmt = _mirror_backup(db_path, name, remote_dir)
        if rmt:
            mirrored.append(rmt)
        else:
            mirror_note = "；异地备份目录不可用（%s），本次仅保留本地副本" % remote_dir
        # 异地目录同样按保留策略清理（prefix 一致、目录不同）
        _prune(name.split("%s" % stamp)[0], keep, remote_dir)

    if not made:
        return True, "今日已备份，跳过（--force 可强制重备）；异地副本镜像跳过", ""

    size = os.path.getsize(os.path.join(BACKUP_DIR, made[0])) / 1024 / 1024
    mirror_txt = ("；异地双副本 OK：%d 个（%s）" % (len(mirrored), "、".join(mirrored))) if mirrored else ""
    return True, "已备份：%s（%.1f MB）；保留策略 daily=%d / weekly=%d%s%s" % (
        "、".join(made), size, keep_daily, keep_weekly, mirror_txt, mirror_note), os.path.join(BACKUP_DIR, made[0])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地数据库备份与完整性校验 + 异地双副本")
    ap.add_argument("--check", action="store_true", help="只做完整性校验，不备份")
    ap.add_argument("--force", action="store_true", help="忽略当天已备份的限制")
    ap.add_argument("--keep-daily", type=int, default=30, help="日备份保留天数（默认30=规格书 D1）")
    ap.add_argument("--keep-weekly", type=int, default=4, help="每周全量保留周数（默认4）")
    ap.add_argument("--remote-dir", default=None,
                    help="异地备份目录（D1 双副本，如 E:/backup/stock-backups；默认 "
                         "环境变量 DB_BACKUP_REMOTE_DIR 或 D:/stock-backups）")
    args = ap.parse_args(argv)

    if args.check:
        ok, msg = integrity_check()
        print("[db_backup] 完整性校验：%s —— %s" % ("PASS" if ok else "FAIL", msg))
        return 0 if ok else 1

    ok, msg, path = daily_backup(keep_daily=args.keep_daily, keep_weekly=args.keep_weekly,
                                 force=args.force, remote_dir=args.remote_dir)
    print("[db_backup] %s —— %s" % ("PASS" if ok else "FAIL", msg))
    if path:
        print("[db_backup] 备份文件：%s" % path)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
