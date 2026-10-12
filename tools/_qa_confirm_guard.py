# -*- coding: utf-8 -*-
"""P0-2 / P0-3 的**生产库上**安全验证：只验证"被拒绝"路径，绝不触发成功路径。

对当前生产实例（8765）发：
  · POST /api/demo          无 confirm  → 期望 409，且 9 张表 COUNT 不变
  · POST /api/overseas      reset=true  → 期望 409，且 COUNT 不变
  · POST /api/demo          CT=text/plain（简单请求，CSRF 向量）→ 期望 403/415（不得 200）
  · 再起第二个实例绑同一端口 → 期望退出码 1（单实例保护）

**不**发送带 confirm:true 的请求（那会真清库，成功路径已在副本上验过）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tools" / "_qa_confirm_guard.txt"
DB = ROOT / "viewer" / "app.db"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
TABLES = ["posts", "trades", "positions", "players", "overseas_news",
          "overseas_hotspots", "overseas_sources_status", "stock_mentions", "news"]

L: list[str] = []
_op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def w(s: str = "") -> None:
    L.append(s)


def counts() -> dict:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c = {}
    for t in TABLES:
        try:
            c[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception as e:
            c[t] = f"ERR {e}"
    try:
        c["meta.last_demo_at"] = con.execute(
            "SELECT COALESCE(value,'(无)') FROM meta WHERE key='last_demo_at'").fetchone()
    except Exception:
        c["meta.last_demo_at"] = "(查不到)"
    try:
        c["meta.overseas_last_collect"] = con.execute(
            "SELECT COALESCE(value,'(无)') FROM meta WHERE key='overseas_last_collect'").fetchone()
    except Exception:
        c["meta.overseas_last_collect"] = "(查不到)"
    con.close()
    return c


def post(path: str, payload, ctype: str, origin: str | None = None):
    body = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": ctype}
    if origin:
        h["Origin"] = origin
    req = urllib.request.Request(BASE + path, data=body, method="POST", headers=h)
    try:
        with _op.open(req, timeout=25) as r:
            return r.status, r.read()[:300].decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


w("P0-2 / P0-3 生产库安全验证（只走'被拒绝'路径）")
w("=" * 78)

before = counts()
w("[基线] 9 张表行数")
for k, v in before.items():
    w(f"    {k:<28} {v}")

w("")
w("[1] POST /api/demo（无 confirm，application/json）")
st, body = post("/api/demo", {}, "application/json", origin=BASE)
w(f"    HTTP {st}")
w(f"    body = {body}")

w("")
w("[2] POST /api/overseas（reset=true，无 confirm）")
st2, body2 = post("/api/overseas", {"live": True, "reset": True}, "application/json", origin=BASE)
w(f"    HTTP {st2}")
w(f"    body = {body2}")

w("")
w("[3] POST /api/demo（Content-Type: text/plain —— 简单请求，不触发预检的 CSRF 向量）")
st3, body3 = post("/api/demo", {}, "text/plain")
w(f"    HTTP {st3}")
w(f"    body = {body3}")

w("")
w("[4] POST /api/demo（恶意 Origin: https://evil.example）")
st4, body4 = post("/api/demo", {}, "application/json", origin="https://evil.example")
w(f"    HTTP {st4}")
w(f"    body = {body4}")

after = counts()
w("")
w("[5] 前后对比（必须完全一致）")
same = True
for k in before:
    a, b = before[k], after[k]
    flag = "OK " if a == b else "差异"
    if a != b:
        same = False
    w(f"    {flag} {k:<28} {a}  ->  {b}")
w(f"    结论：{'完全一致，未被触碰' if same else '★ 有变化，必须立即排查 ★'}")

w("")
w("[6] 单实例保护：再起一个实例绑 8765")
env = dict(os.environ)
env["VIEWER_PORT"] = str(PORT)
py = str(ROOT / ".venv" / "Scripts" / "python.exe")
try:
    p = subprocess.run([py, "-m", "viewer.server"], cwd=str(ROOT), env=env,
                       capture_output=True, timeout=40)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    w(f"    退出码 = {p.returncode}（期望 1）")
    w("    输出：")
    for line in out.strip().splitlines()[-8:]:
        w("      " + line)
except subprocess.TimeoutExpired:
    w("    ★ 第二个实例没有退出（40s 超时）→ 单实例保护未生效 ★")

w("")
w("[7] 判定")
w(f"    /api/demo 无 confirm 被拒 (409)      : {'PASS' if st == 409 else 'FAIL -> ' + str(st)}")
w(f"    /api/overseas reset 无 confirm 被拒  : {'PASS' if st2 == 409 else 'FAIL -> ' + str(st2)}")
w(f"    text/plain 非简单请求未被放行        : {'PASS' if st3 in (403, 415, 400) else 'FAIL -> ' + str(st3)}")
w(f"    跨站 Origin 被拒                     : {'PASS' if st4 in (403, 415, 400, 409) else 'FAIL -> ' + str(st4)}")
w(f"    数据未被触碰                         : {'PASS' if same else 'FAIL'}")

OUT.write_text("\n".join(L), encoding="utf-8")
print("done")
