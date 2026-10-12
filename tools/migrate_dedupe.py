"""一次性迁移脚本：业务表幂等去重 + 唯一索引 + 示例/孤儿残留清理。

安全约束：
- 默认禁止直接对生产库 viewer/app.db 执行；必须显式传 --force-live 才允许。
- 迁移前自动做一次 sqlite 在线备份（sqlite3.Connection.backup，含 WAL 未落盘内容）。
- 全程只在指定 --db 上操作；本脚本不触碰运行中的 8765 服务、不修改生产库本体。

用法：
  python tools/migrate_dedupe.py --db %TEMP%\\_fixcopy.db
  python tools/migrate_dedupe.py --db viewer/app.db --force-live   # 仅 lead 允许
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer.db import Database, BackupFailed  # noqa: E402

LIVE_DB = (ROOT / "viewer" / "app.db").resolve()


def online_backup(src_path: str) -> str:
    """用 sqlite 在线备份 API 复制整库（含 WAL），返回备份路径。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = str(Path(src_path).resolve()) + f".migrate-bak-{stamp}"
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def size_of(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return -1


def _print_report(rep: dict, label: str):
    print(f"  === {label} 报告 ===")
    before = rep.get("before", {})
    after = rep.get("after", {})
    print("  业务表去重删除：", rep.get("dedup_removed"))
    for t in ("news", "posts", "trades", "raw_pages",
              "persons", "stock_mentions", "topics", "overseas_news", "overseas_mentions"):
        b = before.get(t)
        a = after.get(t)
        if b is not None and a is not None:
            print(f"    {t:18s} {b:>8d} -> {a:>8d}  (Δ {a - b:+d})")
    for k in ("persons_removed", "stock_mentions_removed", "topics_before",
              "topics_after", "overseas_news_removed", "overseas_mentions_removed"):
        if k in rep:
            print(f"    {k:24s} = {rep[k]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.environ.get("TEMP", "."), "_fixcopy.db"),
                    help="目标库路径（默认 %TEMP%\\_fixcopy.db）")
    ap.add_argument("--force-live", action="store_true",
                    help="允许对生产库 viewer/app.db 执行（默认禁止）")
    ap.add_argument("--no-residual", action="store_true",
                    help="只做业务表去重+索引，不做 persons/stock_mentions/topics/overseas 清理")
    ap.add_argument("--fresh", action="store_true",
                    help="若目标库已存在，先从生产库重新拷贝一份（覆盖旧副本）")
    args = ap.parse_args()

    target = Path(args.db).resolve()
    if target == LIVE_DB and not args.force_live:
        sys.exit(f"[拒绝] 默认禁止直接迁移生产库 {LIVE_DB}；如需执行请加 --force-live（仅限 lead 指令）。")

    # 准备副本：不存在（或 --fresh）时，从生产库做在线备份得到一致副本
    if not target.exists() or args.fresh:
        if not LIVE_DB.exists():
            sys.exit(f"[错误] 生产库不存在：{LIVE_DB}")
        print(f"[0/6] 从生产库拷贝一致副本 -> {target} ...")
        _replace_with_backup(str(LIVE_DB), str(target))
    else:
        print(f"[0/6] 复用已存在的副本 {target}")

    print(f"[1/6] 在线备份（迁移前回滚点） {target} ...")
    bak = online_backup(str(target))
    print(f"      备份 -> {bak}")

    size_before = size_of(str(target))

    # 打开副本（__init__ 不会自动去重，迁移由下面显式调用，绝不误伤生产库）
    db = Database(str(target))
    print(f"[2/6] 迁移前计数：{db.counts()}")

    print("[3/6] 执行迁移（首次）...")
    rep = db.migrate_dedupe(apply_residual=not args.no_residual)
    _print_report(rep, "首次迁移")

    print("[4/6] 幂等验证：再次执行迁移（应删除 0 行、不报错）...")
    rep2 = db.migrate_dedupe(apply_residual=not args.no_residual)
    dr2 = rep2.get("dedup_removed", {})
    total2 = sum(dr2.values()) if isinstance(dr2, dict) else 0
    print(f"      二次去重删除行数 = {total2}（期望 0）")
    assert total2 == 0, "幂等校验失败：第二次迁移仍删除了行"

    print("[5/6] 唯一索引生效验证：手工 INSERT 一条重复 news，应被 IGNORE...")
    c = db.conn
    n_before = c.execute("SELECT COUNT(*) n FROM news").fetchone()["n"]
    c.execute(
        "INSERT OR IGNORE INTO news (site,title,url,publish_time,summary,source) "
        "SELECT site,title,url,publish_time,summary,source FROM news LIMIT 1"
    )
    db.conn.commit()
    n_after = c.execute("SELECT COUNT(*) n FROM news").fetchone()["n"]
    print(f"      重复 INSERT 前后 news 计数：{n_before} -> {n_after}（期望相等）")
    assert n_before == n_after, "唯一索引未生效：重复 news 未被忽略"

    # 回收被删除行释放的页，使文件体积真实下降
    print("      VACUUM 回收空闲页 ...")
    db.conn.execute("VACUUM")
    db.conn.commit()

    size_after = size_of(str(target))
    print(f"[6/6] 文件体积：{size_before} -> {size_after} 字节 "
          f"（减少 {size_before - size_after}）")
    db.close()
    print("迁移完成。")


def _replace_with_backup(src: str, dst: str):
    """用「从 src 在线备份」得到的内容覆盖 dst（src 不被修改）。"""
    if os.path.exists(dst):
        os.remove(dst)
    src_conn = sqlite3.connect(src)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


if __name__ == "__main__":
    main()
