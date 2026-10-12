# -*- coding: utf-8 -*-
"""查看器整体冒烟：隔离副本 + 临时端口，逐一打通所有 GET 接口，
并验证修复后的 POST /api/demo 二次确认契约（409 未确认 / 200 已确认）。
绝不触碰真实 app.db。"""
import os, sys, json, time, shutil, tempfile, subprocess, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix="smoke-"))
shutil.copy2(ROOT / "viewer" / "app.db", tmp / "app.db")
port = 8798
env = dict(os.environ, VIEWER_DB=str(tmp / "app.db"), VIEWER_PORT=str(port))
proc = subprocess.Popen([sys.executable, "-m", "viewer.server"], cwd=str(ROOT), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{port}"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 显式绕过代理


def req(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with opener.open(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, None


ready = False
for _ in range(80):
    try:
        req("/api/summary"); ready = True; break
    except Exception:
        time.sleep(0.25)
assert ready, "服务未就绪"

fails = []
try:
    gets = ["/api/summary", "/api/stocks", "/api/operating", "/api/persons", "/api/sentiment",
            "/api/topics", "/api/players", "/api/positions", "/api/consensus?min_holders=2",
            "/api/posts?limit=20", "/api/login_status", "/api/stock_names",
            "/api/overseas_sentiment", "/api/overseas_hotspots", "/api/overseas_sources",
            "/api/detail_list?kind=trades", "/api/stock?symbol=002594"]
    print("--- GET 接口 ---")
    for p in gets:
        c, d = req(p)
        good = (c == 200 and d is not None and not (isinstance(d, dict) and d.get("error")))
        print(f"  {'✓' if good else '✗'} {c:>3}  {p}")
        if not good:
            fails.append(p)

    # 取一个真实选手/人物名做详情接口冒烟
    _, pl = req("/api/players")
    nm = pl[0]["name"] if isinstance(pl, list) and pl else ""
    if nm:
        c, d = req(f"/api/player?name={urllib.parse.quote(nm)}")
        print(f"  {'✓' if c == 200 else '✗'} {c:>3}  /api/player?name={nm}")
        if c != 200:
            fails.append("player")

    print("--- POST /api/demo 二次确认契约 ---")
    c, d = req("/api/demo", "POST", {})
    good = (c == 409 and isinstance(d, dict) and d.get("error") == "destructive_operation")
    print(f"  {'✓' if good else '✗'} {c:>3}  未 confirm → 期望 409 destructive_operation（且不碰库）")
    if not good:
        fails.append("demo-409")

    before = req("/api/summary")[1]["counts"]
    c, d = req("/api/demo", "POST", {"confirm": True})
    good = (c == 200 and isinstance(d, dict) and d.get("ok"))
    print(f"  {'✓' if good else '✗'} {c:>3}  confirm=True → 期望 200（隔离副本上执行）")
    if not good:
        fails.append(f"demo-200:{d}")
    after = req("/api/summary")[1]["counts"]
    print(f"  载入前后 players: {before.get('players')} → {after.get('players')}；"
          f"trades: {before.get('trades')} → {after.get('trades')}")

    # 载入后再验一次明细一致性
    c, d = req("/api/detail_list?kind=players&limit=1000")
    consistent = (d["total"] == after.get("players"))
    print(f"  {'✓' if consistent else '✗'} 载入后 players 卡片={after.get('players')} 明细={d['total']}")
    if not consistent:
        fails.append("post-demo-consistency")

    print("\n" + ("✅ 冒烟全部通过：查看器已恢复可用" if not fails else f"❌ 失败项：{fails}"))
    sys.exit(0 if not fails else 1)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)
