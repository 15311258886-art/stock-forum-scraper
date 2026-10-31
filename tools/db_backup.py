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
  · 保留策略：每日 1 份留 `--keep-daily`（默认 3）份；每周（周一）1 份留 `--keep-weekly`（默认 2）份。
    2026-10-31 从 30/4 收紧：库体积 9GB 后原策略最坏会占 270GB（实测已堆到 32GB）。
  · 只清理**本工具自己产生的**文件（`app.db.daily-*` / `app.db.weekly-*`），
    不动历史手工快照 `app.db.pre-*`（那些是改造前的还原点，需人工确认后再清）。
  · 异地镜像用同样在线备份 API 直写异地库文件（不复制本地备份文件），一致性有保证；
    异地目录不可写时降级为"仅本地"并如实告警，不阻断主流程。
  · `--check` 只做 `PRAGMA integrity_check`，不写任何文件。

## 空间闸（2026-11-01 P1-4，库涨到 9.5 GB 后必须有的护栏）

  ① **备份前量盘子**：目标盘剩余空间 < 「库大小 × 1.3」→ 直接拒绝执行（退出码 2）。
     为什么是硬拒绝而不是"尽力而为"：在线备份 API 写到一半空间耗尽，留下的是一个
     大小看着正常、内容却截断的 .db —— 它像个备份，恢复时才发现是坏的。
     亲手造一个假备份，比这次没备份危险得多。
     1.3 的系数是给「在线备份的临时页 + 文件系统碎片 + WAL」留的余量。
  ② **已有备份只提示不删**：份数/占用超过阈值时在输出里明确写出来，但**不自动删**。
     删备份是不可逆的破坏性操作，不该由一个每日自动跑的脚本替你决定。

用法：
    .venv\\Scripts\\python.exe tools/db_backup.py            # 每日备份（幂等：当天已备则跳过）
    .venv\\Scripts\\python.exe tools/db_backup.py --check    # 只校验完整性
    .venv\\Scripts\\python.exe tools/db_backup.py --force    # 忽略"当天已备"限制
    .venv\\Scripts\\python.exe tools/db_backup.py --remote-dir "E:/backup/stock-backups"
退出码：0=成功（校验模式下=库完好）；1=失败/库损坏；2=目标盘空间不足，拒绝执行。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "viewer", "app.db")
BACKUP_DIR = os.path.join(ROOT, "viewer", "backups")

# ---- 空间闸与备份存量闸（2026-11-01 P1-4）----
# 目标盘至少要有「库大小 × 这个系数」。留 30% 余量给在线备份的临时页、WAL 与文件系统碎片。
SPACE_FACTOR = 1.3
# 备份目录里文件数超过这个值，只在输出里提示，绝不自动删（见模块 docstring ②）
BACKUP_COUNT_WARN = 3
# 拒绝执行时消息的固定前缀：main() 靠它把退出码区分成 2（拒绝）而不是 1（失败）
SPACE_REFUSED_TAG = "[空间不足]"


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


def _mb(n) -> str:
    """字节转人读（1 KB = 1024 B）。备份相关提示统一用它，别在文案里混用 MB/MiB。"""
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return ("%.0f %s" % (f, unit)) if unit == "B" else ("%.1f %s" % (f, unit))
        f /= 1024.0
    return "%.1f TB" % f


def _disk_free(path: str) -> int | None:
    """path 所在盘剩余字节；path 还不存在时向上找最近的存在祖先（备份目录常是新建的）。"""
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _preflight_space(db_path: str, target_dir: str) -> tuple:
    """目标盘够不够写一份完整备份。返回 (ok, message)。ok=False 时调用方必须放弃本次备份。

    读不出盘信息（网络盘/权限）时放行，但把"测不出来"如实写进 message ——
    不能因为量不出来就假装量过了。
    """
    if not target_dir:
        return True, ""          # 未配置异地目录：没有目标盘可量
    try:
        db_size = os.path.getsize(db_path)
    except OSError as e:
        return False, "%s 读不到库大小（%s），无法确认目标盘是否够用" % (SPACE_REFUSED_TAG, e)
    need = int(db_size * SPACE_FACTOR)
    free = _disk_free(target_dir)
    if free is None:
        return True, "；注意：%s 剩余空间读不出来，本次未做空间检查" % target_dir
    if free < need:
        return False, ("%s 空间不足：需要 %s（库 %s × %.1f），实际可用 %s。"
                       "已拒绝执行 —— 写一半空间耗尽会产出一个截断的假备份，比不备份更危险。"
                       % (SPACE_REFUSED_TAG, _mb(need), _mb(db_size), SPACE_FACTOR, _mb(free)))
    return True, ""


def backup_inventory(d: str | None = None) -> dict:
    """已有备份的份数/总占用/最老最新。**只报告，不删** —— 删备份是不可逆操作。

    统计该目录下所有普通文件（含历史手工快照 app.db.pre-*），因为占磁盘的是它们全部，
    不只是本工具产生的 daily/weekly。
    """
    d = d or BACKUP_DIR
    info = {"dir": d, "files": 0, "bytes": 0, "oldest": None, "newest": None,
            "managed": 0}
    if not os.path.isdir(d):
        return info
    for entry in os.scandir(d):
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat()
        except OSError:
            continue
        info["files"] += 1
        info["bytes"] += st.st_size
        if entry.name.startswith(("app.db.daily-", "app.db.weekly-")):
            info["managed"] += 1
        if info["oldest"] is None or st.st_mtime < info["oldest"]:
            info["oldest"] = st.st_mtime
        if info["newest"] is None or st.st_mtime > info["newest"]:
            info["newest"] = st.st_mtime
    return info


def _inventory_note(d: str | None = None) -> str:
    """存量提示（不删）。超过阈值就明说，并给出人去处理的入口。"""
    inv = backup_inventory(d)
    if inv["files"] <= BACKUP_COUNT_WARN:
        return ""
    return ("；注意：%s 已有 %d 个文件 / %s（超过建议保留 %d 份）"
            "—— 只提示不自动删，确认后可手工清理" % (
                inv["dir"], inv["files"], _mb(inv["bytes"]), BACKUP_COUNT_WARN))


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

    # 先算清楚这次真正要写哪几份：若全部已存在且没 --force，那就没什么要写的，
    # 也就没有任何理由因为"空间不足"去拦一个只读的幂等调用。
    planned = [t for t in targets
               if force or not os.path.exists(os.path.join(BACKUP_DIR, t[0]))]
    if not planned:
        return True, ("今日已备份，跳过（--force 可强制重备）；异地副本镜像跳过%s"
                      % _inventory_note()), ""

    # 空间闸（硬）：本地备份目录不够「库大小×1.3」就不开始 —— 写一半的备份是假备份
    space_ok, space_msg = _preflight_space(db_path, BACKUP_DIR)
    if not space_ok:
        return False, space_msg, ""

    made = []
    mirror_note = space_msg            # 可能带着"剩余空间读不出来"的说明
    mirrored = []
    for name, keep in planned:
        dst = os.path.join(BACKUP_DIR, name)
        _online_backup(db_path, dst)
        made.append(name)
        _prune(name.split("%s" % stamp)[0], keep)
        # D1 异地双副本：同一份备份镜像到异地目录（网盘/外接盘），保留策略与本地一致。
        # 异地也先量盘子：不够就降级为「仅本地」，绝不往异地写半个副本（同样是假备份问题）。
        r_ok, r_msg = _preflight_space(db_path, remote_dir)
        rmt = _mirror_backup(db_path, name, remote_dir) if r_ok else None
        if rmt:
            mirrored.append(rmt)
        else:
            mirror_note = "；异地备份不可用（%s）%s，本次仅保留本地副本" % (
                remote_dir, "" if r_ok else "：" + r_msg)
        # 异地目录同样按保留策略清理（prefix 一致、目录不同）
        _prune(name.split("%s" % stamp)[0], keep, remote_dir)

    # planned 非空 ⇒ 循环里至少执行过一次 _online_backup（异常会直接抛出），made 必非空
    size = os.path.getsize(os.path.join(BACKUP_DIR, made[0])) / 1024 / 1024
    mirror_txt = ("；异地双副本 OK：%d 个（%s）" % (len(mirrored), "、".join(mirrored))) if mirrored else ""
    return True, "已备份：%s（%.1f MB）；保留策略 daily=%d / weekly=%d%s%s%s" % (
        "、".join(made), size, keep_daily, keep_weekly, mirror_txt, mirror_note,
        _inventory_note()), os.path.join(BACKUP_DIR, made[0])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="本地数据库备份与完整性校验 + 异地双副本")
    ap.add_argument("--check", action="store_true", help="只做完整性校验，不备份")
    ap.add_argument("--force", action="store_true", help="忽略当天已备份的限制")
    ap.add_argument("--keep-daily", type=int, default=3,
                    help="日备份保留份数（默认3）。2026-10-31 由 30 收紧：库已涨到 9GB，"
                         "30 份等于 270GB，磁盘扛不住；3 份足够覆盖「今天改坏了」的回滚窗口")
    ap.add_argument("--keep-weekly", type=int, default=2,
                    help="每周全量保留份数（默认2）。原为 4，理由同上")
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
    if not ok:
        # 空间不足是"主动拒绝"，和被写坏的失败不是一回事：给独立退出码，
        # 让 cron/auto_update 能区分"再等等（磁盘会腾出来）"和"这里真坏了"。
        return 2 if msg.startswith(SPACE_REFUSED_TAG) else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
