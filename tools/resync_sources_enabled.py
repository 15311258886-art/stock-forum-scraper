# -*- coding: utf-8 -*-
"""把 overseas_sources_status.enabled 按 config/overseas_sources.yaml 注册表校准。

背景：2026-09-22 从 app.db.bak-20260922-230629 回滚时，该快照生成于 db.py 增加
`enabled` 列之前，于是 ALTER TABLE 用 DEFAULT 0 把 41 行全填成 0；
而注册表实际是 28 开 / 13 关。本脚本只更新 `enabled` 一列，
**不改 status 字符串**（状态文案的语义修复需与前端分组逻辑一起改，另行安排）。

用法：  .venv\\Scripts\\python.exe tools\\resync_sources_enabled.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = Path(os.environ.get("VIEWER_DB") or (ROOT / "viewer" / "app.db"))

from overseas.sources import load_overseas_config  # noqa: E402

cfg = load_overseas_config()
registry = {s.get("key"): bool(s.get("enabled")) for s in cfg.get("sources", []) if s.get("key")}
print(f"[registry] config/overseas_sources.yaml 共 {len(registry)} 个源，"
      f"enabled=1 共 {sum(1 for v in registry.values() if v)}，"
      f"enabled=0 共 {sum(1 for v in registry.values() if not v)}")

con = sqlite3.connect(str(DB))
con.execute("PRAGMA busy_timeout=8000")
before = {k: e for k, e in con.execute(
    "SELECT source_key, COALESCE(enabled, -1) FROM overseas_sources_status").fetchall()}
print(f"[db] 校准前：enabled=1 共 {sum(1 for v in before.values() if v == 1)}，"
      f"enabled=0 共 {sum(1 for v in before.values() if v == 0)}，"
      f"共 {len(before)} 行")

changed, missing = [], []
for key, en in registry.items():
    if key not in before:
        missing.append(key)
        continue
    want = 1 if en else 0
    if before[key] != want:
        con.execute("UPDATE overseas_sources_status SET enabled=? WHERE source_key=?",
                    (want, key))
        changed.append(f"{key}: {before[key]} -> {want}")

extra = [k for k in before if k not in registry]
con.commit()
after = dict(con.execute(
    "SELECT source_key, COALESCE(enabled, -1) FROM overseas_sources_status").fetchall())
con.close()

print(f"[db] 校准后：enabled=1 共 {sum(1 for v in after.values() if v == 1)}，"
      f"enabled=0 共 {sum(1 for v in after.values() if v == 0)}，共 {len(after)} 行")
print(f"[diff] 实际改写 {len(changed)} 行")
for c in changed:
    print("   " + c)
if missing:
    print(f"[warn] 注册表有、库中没有的源（{len(missing)}）：{missing}")
if extra:
    print(f"[warn] 库中有、注册表没有的源（{len(extra)}）：{extra}")
