# -*- coding: utf-8 -*-
"""列 staging 里 空白 symbol_name 行 + 全部 xueqiu 行。只读。"""
from __future__ import annotations
import json, sys, collections
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
BASE = Path(r"F:\AI Agent部署知识库\2026-10-07-14-13-22\.workbuddy\douyin\采集数据")
log = open(ROOT / "tools" / "_blank_scan.log", "w", encoding="utf-8")
sys.stdout = log
rows = [json.loads(l) for l in (BASE / "trades_v2_staging.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
blank = [r for r in rows if not (r.get("symbol_name") or "").strip()]
print("空白 symbol_name 行数 =", len(blank))
for r in blank:
    print("   pid=%r site=%s sym=%r act=%s" % (r.get("post_id"), r.get("site"), r.get("symbol"), r.get("action")))
print("\n空 symbol 的行数 =", len([r for r in rows if not (r.get("symbol") or "").strip()]))
print("\n全部 xueqiu 行（%d）:" % len([r for r in rows if r.get("site") == "xueqiu"]))
for r in rows:
    if r.get("site") == "xueqiu":
        print("   pid=%r sym=%r name=%r act=%s" % (r.get("post_id"), r.get("symbol"), r.get("symbol_name"), r.get("action")))
