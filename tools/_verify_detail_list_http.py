# -*- coding: utf-8 -*-
"""端到端校验：起一个**隔离实例**，用真实 HTTP 打 /api/detail_list，
证明「统计卡片数字 → 点开看明细」链路在 HTTP 层成立（不只是单元级）。

隔离：复制一份 app.db 到临时目录，VIEWER_DB 指向副本、端口 8799，
绝不触碰真实 app.db，也不打扰用户可能在跑的 8765 实例。
"""
import os, sys, json, time, shutil, tempfile, subprocess, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

tmp = Path(tempfile.mkdtemp(prefix="verify-http-"))
shutil.copy2(ROOT / "viewer" / "app.db", tmp / "app.db")
port = 8799
env = dict(os.environ, VIEWER_DB=str(tmp / "app.db"), VIEWER_PORT=str(port))
proc = subprocess.Popen([sys.executable, "-m", "viewer.server"], cwd=str(ROOT), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{port}"


def get(path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


ready = False
for _ in range(80):
    try:
        get("/api/summary"); ready = True; break
    except Exception:
        time.sleep(0.25)
assert ready, "服务未在预期时间内就绪"

try:
    _, summary = get("/api/summary")
    counts = summary["counts"]
    kinds = ["raw_pages", "posts", "trades", "news", "players",
             "positions", "persons", "stock_mentions", "topics"]
    allok = True
    print(f"{'kind':16}{'卡片':>6}{'明细total':>10}{'行数':>6}  一致?")
    for k in kinds:
        _, d = get(f"/api/detail_list?kind={k}&limit=1000")
        same = d["total"] == counts.get(k)
        rows_ok = d["returned"] == min(counts.get(k, 0), 1000)
        allok = allok and same and rows_ok
        print(f"{k:16}{counts.get(k):>6}{d['total']:>10}{d['returned']:>6}  "
              f"{'✓' if (same and rows_ok) else '✗'}")

    _, d = get("/api/detail_list?kind=trades&limit=1000")
    ex = d["rows"][0]
    print("trades 首行样例:", {q: ex.get(q) for q in ("author", "action", "symbol", "symbol_name", "price")})
    _, d = get("/api/detail_list?kind=positions&limit=1000")
    ex = d["rows"][0]
    print("positions 首行样例:", {q: ex.get(q) for q in ("player", "symbol", "symbol_name", "ratio")})

    try:
        get("/api/detail_list?kind=trades&limit=abc")
        print("非法 limit → 未按预期返回 400  ✗"); allok = False
    except urllib.error.HTTPError as e:
        print("非法 limit → HTTP", e.code, "(期望 400)")

    print("\n" + ("✅ 端到端校验通过：HTTP 层数字与明细一致" if allok else "❌ 端到端校验失败"))
    sys.exit(0 if allok else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)
