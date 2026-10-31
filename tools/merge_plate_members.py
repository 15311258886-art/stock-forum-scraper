# -*- coding: utf-8 -*-
"""把 staging 的 17 板块成分股并入正式库 viewer/app.db。

幂等：逐板块 DELETE(board=?) 再 INSERT。
对 1GB 生产库做了保护：busy_timeout 等待 viewer 服务释放写锁；单事务提交。

用法：.venv/Scripts/python.exe tools/merge_plate_members.py
"""
from __future__ import annotations
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "viewer" / "plate_members_staging.db"
LIVE = ROOT / "viewer" / "app.db"


def main() -> int:
    if not STAGING.exists():
        print(f"[error] 暂存库不存在：{STAGING}")
        return 2
    if not LIVE.exists():
        print(f"[error] 正式库不存在：{LIVE}")
        return 2

    scon = sqlite3.connect(str(STAGING), timeout=30)
    boards = scon.execute(
        "select board, board_code, code, name, updated_at from plate_members"
    ).fetchall()
    scon.close()
    print(f"==> 暂存库读取 {len(boards)} 行")

    lcon = sqlite3.connect(str(LIVE), timeout=120)  # 等 viewer 释放锁，最多 2 分钟
    lcon.execute("PRAGMA busy_timeout=120000")
    lcon.execute("""CREATE TABLE IF NOT EXISTS plate_members (
        code TEXT, name TEXT, board TEXT, board_code TEXT, updated_at TEXT)""")

    # 按板块分组
    from collections import defaultdict
    by_board = defaultdict(list)
    for board, bcode, code, name, ts in boards:
        by_board[board].append((code, name, board, bcode, ts))

    total = 0
    for board in sorted(by_board):
        rows = by_board[board]
        lcon.execute("DELETE FROM plate_members WHERE board=?", (board,))
        lcon.executemany(
            "INSERT INTO plate_members(code,name,board,board_code,updated_at) VALUES(?,?,?,?,?)",
            rows)
        total += len(rows)
        print(f"  [ok] {board:>4} : {len(rows):>4} 只")
    lcon.commit()
    lcon.close()
    print(f"\n==> 并入正式库完成，共 {total} 行；viewer/app.db.plate_members 已就绪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
