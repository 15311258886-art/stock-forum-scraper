# -*- coding: utf-8 -*-
"""A 点口径字段落点复核（正确端点是 /api/player，上一轮脚本误用了 /api/person）。

在隔离副本 qa_iso/app.db 上：
 - in-process 直调 Database.player_detail()，确认 hold_days_basis / hold_days_mode 真的写入
 - 再经 HTTP GET /api/player?name=... 确认字段穿过 JSON 层可被前端读到
结果追加进 tools/_qa_fix_backend2.txt。
"""
from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
ISO_DIR = Path(os.environ.get("TEMP") or "/tmp") / "qa_iso"
ISO_DB = ISO_DIR / "app.db"
PORT = 8799

OUT: list[str] = []
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def p(*a):
    line = " ".join(str(x) for x in a)
    OUT.append(line)
    print(line)


sys.path.insert(0, str(ROOT))
os.environ["VIEWER_DB"] = str(ISO_DB)
from viewer.db import Database  # noqa: E402

p("")
p("=" * 78)
p("[A 点 复核] hold_days 口径字段真实落点（端点应为 /api/player，非 /api/person）")
p("=" * 78)

c = sqlite3.connect(str(ISO_DB))
who = c.execute("SELECT name FROM players ORDER BY rank_position LIMIT 1").fetchone()[0]
c.close()

db = Database(str(ISO_DB))
d = db.player_detail(who, recent_days=5)
p(f"in-process player_detail({who!r}) 顶层键 = {sorted(d.keys())}")
p(f"    hold_days_basis          = {d.get('hold_days_basis')!r}")
p(f"    holdings 条数 = {len(d['holdings'])} / sells 条数 = {len(d['sells'])} / trades 条数 = {len(d['trades'])}")
modes_h = sorted({it.get("hold_days_mode") for it in d["holdings"]})
modes_s = sorted({it.get("hold_days_mode") for it in d["sells"]})
p(f"    holdings 的 hold_days_mode 取值 = {modes_h}")
p(f"    sells    的 hold_days_mode 取值 = {modes_s}")
p(f"    每条记录都带 hold_days_mode = "
  f"{all('hold_days_mode' in it for it in d['holdings'] + d['sells'])}")
p("    样本（持仓前 3 条）：")
for it in d["holdings"][:3]:
    p(f"      {it.get('symbol'):<8} hold_since={it.get('hold_since')!r:<20} "
      f"hold_days={it.get('hold_days')}  mode={it.get('hold_days_mode')!r}")
if d["sells"]:
    p("    样本（卖出前 3 条）：")
    for it in d["sells"][:3]:
        p(f"      {it.get('symbol'):<8} hold_since={it.get('hold_since')!r:<20} "
          f"sell_time={it.get('sell_time')!r:<20} hold_days={it.get('hold_days')}  "
          f"mode={it.get('hold_days_mode')!r}")

# ---- 经 HTTP 复核字段可被前端读到 ----
env = dict(os.environ)
env["VIEWER_DB"] = str(ISO_DB)
env["VIEWER_PORT"] = str(PORT)
logp = ROOT / "tools" / "_qa_fix_backend2_server_a.log"
logf = open(logp, "w", encoding="utf-8", errors="replace")
proc = subprocess.Popen([PY, "-m", "viewer.server"], cwd=str(ROOT), env=env,
                        stdout=logf, stderr=subprocess.STDOUT)
try:
    t0 = time.time()
    up = False
    while time.time() - t0 < 30:
        try:
            with OPENER.open(f"http://127.0.0.1:{PORT}/api/summary", timeout=3) as r:
                up = r.status == 200
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    p(f"    临时隔离实例（8799）已启动 = {up}")
    with OPENER.open(f"http://127.0.0.1:{PORT}/api/player?name="
                     + urllib.parse.quote(who), timeout=20) as r:
        body = json.loads(r.read().decode("utf-8"))
    p(f"    GET /api/player?name={who} -> status={r.status}")
    p(f"    hold_days_basis = {body.get('hold_days_basis')!r}")
    hmodes = sorted({it.get("hold_days_mode") for it in body.get("holdings", [])})
    p(f"    holdings 的 hold_days_mode 经 HTTP 取值 = {hmodes}")
    p(f"    /api/person（人物聚合，另一张表 persons）不含该字段 = "
      f"{'hold_days_basis' not in json.loads(OPENER.open(f'http://127.0.0.1:{PORT}/api/person?name=' + urllib.parse.quote(who), timeout=20).read().decode('utf-8'))}")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()
    logf.close()

with open(ROOT / "tools" / "_qa_fix_backend2.txt", "a", encoding="utf-8") as f:
    f.write("\n".join(OUT) + "\n")
print("已追加到 tools/_qa_fix_backend2.txt")
