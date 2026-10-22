# -*- coding: utf-8 -*-
"""Schema 迁移机制（P1-36）。

此前全仓 `ensure_schema` 在 import 时顺序执行裸 `PRAGMA table_info → ALTER` 补丁，
导致「线上表结构 = 哪个代码版本的哪串补丁」无法回答。本模块引入 `PRAGMA user_version`
整数版本 + 有序迁移清单，迁移前自动快照（复用 tools/db_backup 若可用）。

设计原则（与红线一致）：
  · 所有建表用 `CREATE TABLE IF NOT EXISTS`，迁移幂等，可重复跑。
  · 迁移按 (version, callable) 有序执行；已应用版本跳过。
  · 迁移前自动快照数据库（兜底回滚）。
  · 不删除既有 ensure_schema 调用（大工程留待后续）；本机制作为「新 schema 改动的安全落点」。

用法：
  from migrate import migrate
  migrate(DB.conn)   # 在 boot() 早期、任何业务读写之前调用
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Callable, List, Tuple

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("migrate")

# 当前 schema 版本。每次新增迁移 +1。
SCHEMA_VERSION = 1

# 迁移清单：[(version, name, callable)]，callable 接收 sqlite3.Connection。
MIGRATIONS: List[Tuple[int, str, Callable[[sqlite3.Connection], None]]] = []


def _register(version: int, name: str):
    def _dec(fn: Callable[[sqlite3.Connection], None]):
        MIGRATIONS.append((version, name, fn))
        return fn
    return _dec


def get_user_version(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except Exception:  # noqa: BLE001
        return 0


def set_user_version(conn: sqlite3.Connection, v: int) -> None:
    conn.execute("PRAGMA user_version=%d" % int(v))
    conn.commit()


def _snapshot(conn: sqlite3.Connection) -> None:
    """迁移前自动快照（尽力而为，失败不阻断迁移）。"""
    try:
        # 复用既有在线备份工具（若存在）做安全落点
        backup_script = ROOT / "tools" / "db_backup.py"
        if backup_script.exists():
            import importlib.util
            spec = importlib.util.spec_from_file_location("db_backup", str(backup_script))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            if hasattr(mod, "snapshot"):
                mod.snapshot(reason="pre-migrate")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        logging.exception("迁移前快照失败（已忽略，继续迁移）")


# ── 迁移实现（幂等；每个都先判断是否存在再动手）──

@_register(1, "ensure_core_meta_table")
def _m1(conn: sqlite3.Connection) -> None:
    """确保所有模块共用的 meta 键值表存在（幂等）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta ("
        "  k TEXT PRIMARY KEY,"
        "  v TEXT,"
        "  updated_at TEXT DEFAULT (datetime('now','localtime'))"
        ")"
    )
    conn.commit()


def migrate(conn: sqlite3.Connection, *, force_snapshot: bool = True) -> int:
    """应用所有未执行的迁移，返回最终 user_version。

    conn 必须可写。迁移前自动快照一次（如 force_snapshot）。
    """
    cur = get_user_version(conn)
    target = SCHEMA_VERSION
    if cur >= target:
        return cur
    if force_snapshot:
        _snapshot(conn)
    # 按版本升序执行
    applied = 0
    for version, name, fn in sorted(MIGRATIONS, key=lambda x: x[0]):
        if version <= cur:
            continue
        try:
            logging.info("apply migration v%d: %s", version, name)
            fn(conn)
            set_user_version(conn, version)
            applied += 1
        except Exception:  # noqa: BLE001
            logging.exception("迁移 v%d (%s) 失败", version, name)
            raise
    logging.info("migrate: applied %d migrations → v%d", applied, get_user_version(conn))
    return get_user_version(conn)


if __name__ == "__main__":
    import sys

    db_path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "viewer" / "app.db")
    c = sqlite3.connect(db_path)
    print("before:", get_user_version(c))
    print("after :", migrate(c))
