# -*- coding: utf-8 -*-
"""第二批后端修复（P0-2 / P0-3 / 隔离能力）隔离副本验收。

硬约束：绝不对 8765 的生产库 viewer/app.db 调用 /api/demo 或 POST /api/overseas。
做法：把 app.db 用 sqlite3 backup 复制到 %TEMP%\\qa_iso\\app.db，服务以
      VIEWER_DB / VIEWER_PORT=8799 起在副本上，全部破坏性验证只打副本。

验证四项行为：
  ① 不带 confirm 的 POST /api/demo -> 409 且数据行数不变（前后各 COUNT 一次）
  ② 带 confirm:true 时先出现 qa_iso/backups/app.db.pre-demo-* 快照，再清空重灌
  ③ VIEWER_DB 生效（服务读副本而非 viewer/app.db；生产库行数不变）
  ④ 端口被占时第二个实例退出码 1 且打印中文提示

报告写入 tools/_qa_fix_backend2.txt。
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
PROD_DB = ROOT / "viewer" / "app.db"
ISO_DIR = Path(os.environ.get("TEMP") or "/tmp") / "qa_iso"
ISO_DB = ISO_DIR / "app.db"
PORT = 8799

OUT: list[str] = []


def p(*a):
    line = " ".join(str(x) for x in a)
    OUT.append(line)
    print(line)


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path: str, payload: dict, port: int = PORT):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with OPENER.open(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return -1, f"<EXC {type(e).__name__}: {e}>".encode()


def get(path: str, port: int = PORT):
    try:
        with OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return -1, f"<EXC {type(e).__name__}: {e}>".encode()


def alive(port: int) -> bool:
    try:
        with OPENER.open(f"http://127.0.0.1:{port}/api/summary", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def wait_up(port: int, timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if alive(port):
            return True
        time.sleep(0.4)
    return False


TABLES = ("posts", "trades", "positions", "players", "overseas_news",
          "overseas_hotspots", "overseas_sources_status", "stock_mentions", "news")


def counts(db_path: Path) -> dict:
    c = sqlite3.connect(str(db_path))
    try:
        out = {}
        for t in TABLES:
            try:
                out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error as e:
                out[t] = f"<{e}>"
        return out
    finally:
        c.close()


def meta(db_path: Path, key: str):
    c = sqlite3.connect(str(db_path))
    try:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None
    finally:
        c.close()


def snaps() -> list[str]:
    d = ISO_DIR / "backups"
    return sorted(x.name for x in d.glob("app.db.pre-demo-*")) if d.exists() else []


# ---------------------------------------------------------------- 准备隔离副本
p("=" * 78)
p(f"第二批后端修复验收（隔离副本 {ISO_DB}；端口 {PORT}；日期 {time.strftime('%Y-%m-%d')}）")
p("=" * 78)
if ISO_DIR.exists():
    shutil.rmtree(ISO_DIR, ignore_errors=True)
ISO_DIR.mkdir(parents=True, exist_ok=True)
src = sqlite3.connect(str(PROD_DB))
dst = sqlite3.connect(str(ISO_DB))
src.backup(dst)          # 事务一致复制（生产库处于 WAL，直接 copy 文件会漏掉未合并改动）
dst.close()
src.close()
prod_before = counts(PROD_DB)
p(f"隔离副本已建立：{ISO_DB}  size={ISO_DB.stat().st_size}")
p(f"生产库基线（全程序末尾复查，必须完全不变）：{prod_before}")

env = dict(os.environ)
env["VIEWER_DB"] = str(ISO_DB)
env["VIEWER_PORT"] = str(PORT)
logp = ROOT / "tools" / "_qa_fix_backend2_server.log"
logf = open(logp, "w", encoding="utf-8", errors="replace")
proc = subprocess.Popen([PY, "-m", "viewer.server"], cwd=str(ROOT), env=env,
                        stdout=logf, stderr=subprocess.STDOUT)

try:
    if not wait_up(PORT):
        p(f"！！隔离实例启动失败，见 {logp}")
        raise SystemExit(1)
    p("隔离实例已启动（-m viewer.server，VIEWER_DB=副本，VIEWER_PORT=8799）")
    st, data = get("/api/summary")
    p(f"/api/summary -> {st}")

    # ---------------------------------------------------------- ③ VIEWER_DB 生效
    p("")
    p("[③] VIEWER_DB 是否真的生效（服务读副本，而不是 viewer/app.db）")
    iso_before = counts(ISO_DB)
    p(f"    副本行数 = {iso_before}")

    # ---------------------------------------------------------- ① 无 confirm -> 409
    p("")
    p("[①] POST /api/demo 不带 confirm -> 期望 409 且数据行数不变")
    st, data = post("/api/demo", {})
    body = json.loads(data.decode("utf-8")) if data and data[:1] == b"{" else data
    p(f"    HTTP {st}  body={body}")
    iso_after = counts(ISO_DB)
    p(f"    副本行数 = {iso_after}")
    p(f"    行数不变 = {iso_after == iso_before}")
    p(f"    meta last_demo_at 未被写入 = {meta(ISO_DB, 'last_demo_at') is None}")

    p("")
    p("[①b] POST /api/overseas {reset:true} 不带 confirm -> 期望 409 且数据行数不变")
    st, data = post("/api/overseas", {"reset": True, "live": False})
    body = json.loads(data.decode("utf-8")) if data and data[:1] == b"{" else data
    p(f"    HTTP {st}  body={body}")
    iso_after2 = counts(ISO_DB)
    p(f"    行数不变 = {iso_after2 == iso_before}")

    p("")
    p("[①c] POST /api/overseas {reset:false} 不带 confirm -> 期望不被 409 拦住（不清空语义）")
    st, data = post("/api/overseas", {"reset": False, "live": False})
    p(f"    HTTP {st}  body={data[:160]!r}")

    # ---------------------------------------------------------- ② 带 confirm -> 快照 + 清空重灌
    p("")
    p("[②] POST /api/demo 带 confirm:true -> 期望先落 qa_iso/backups/app.db.pre-demo-* 再清空重灌")
    s_before = snaps()
    p(f"    调用前快照列表 = {s_before}")
    iso_pre = counts(ISO_DB)
    st, data = post("/api/demo", {"confirm": True})
    body = json.loads(data.decode("utf-8")) if data and data[:1] == b"{" else data
    p(f"    HTTP {st}  ok={body.get('ok') if isinstance(body, dict) else body}")
    s_after = snaps()
    p(f"    调用后快照列表 = {s_after}  （新增 {len(s_after) - len(s_before)} 份）")
    if len(s_after) - len(s_before) == 1:
        new = ISO_DIR / "backups" / s_after[-1]
        p(f"    新快照文件存在 = {new.is_file()}  size={new.stat().st_size if new.is_file() else '-'}")
        p(f"    新快照内容 == 调用前副本 = {counts(new) == iso_pre}")
        p(f"    新快照行数 = {counts(new)}（应等于调用前 {iso_pre}）")
    iso_post = counts(ISO_DB)
    p(f"    重灌后副本行数 = {iso_post}")
    p(f"    数据确实被改写（与调用前不同）= {iso_post != iso_pre}")
    p(f"    meta['last_demo_backup'] = {meta(ISO_DB, 'last_demo_backup')}")
    p(f"    meta['last_demo_at']     = {meta(ISO_DB, 'last_demo_at')}")

    p("")
    p("[②b] 快照保留最近 5 份（连续 6 次带 confirm 调用，第 7 次后应仍为 5 份）")
    for _ in range(5):
        post("/api/demo", {"confirm": True})
        time.sleep(1.05)   # 快照名精确到秒，间隔 1s 避免同秒重名干扰计数
    p(f"    快照份数 = {len(snaps())}（上限 5）")

    # ---------------------------------------------------------- /api/fetch 追溯
    p("")
    p("[P0-2-4] POST /api/fetch -> 写 meta['last_fetch_at']（用不存在的站点，避免真实联网）")
    prev = meta(ISO_DB, "last_fetch_at")
    st, data = post("/api/fetch", {"sites": ["__no_such_site__"]})
    p(f"    HTTP {st}  body={data[:150]!r}")
    now = meta(ISO_DB, "last_fetch_at")
    p(f"    meta['last_fetch_at'] {prev!r} -> {now!r}  已写入 = {bool(now)}")

    # ---------------------------------------------------------- ③ 复查生产库未被动
    p("")
    p("[③ 复查] 生产库 viewer/app.db 是否全程未被动")
    prod_after = counts(PROD_DB)
    p(f"    生产库行数 = {prod_after}")
    p(f"    生产库完全不变 = {prod_after == prod_before}")
    p(f"    backups 目录建在副本旁（非 viewer/）="
      f" {(ISO_DIR / 'backups').is_dir()} ；（ROOT/viewer/backups 不应由本次新建）="
      f" {(ROOT / 'viewer' / 'backups').exists()}")

    # ---------------------------------------------------------- ④ 端口争抢
    p("")
    p("[④] 同一端口起第二个实例 -> 期望退出码 1 + 中文提示")
    env2 = dict(env)   # 同端口 8799，副本库
    r = subprocess.run([PY, "-m", "viewer.server"], cwd=str(ROOT), env=env2,
                       capture_output=True, text=True, timeout=60)
    p(f"    第二个实例 returncode = {r.returncode}")
    outtxt = (r.stdout or "") + (r.stderr or "")
    for ln in outtxt.strip().splitlines():
        p(f"    | {ln}")
    p(f"    退出码为 1 = {r.returncode == 1}")
    p(f"    提示含端口号 8799 = {'8799' in outtxt}")
    p(f"    提示含『已被占用』 = {'已被占用' in outtxt}")

    # ---------------------------------------------------------- A 点口径字段
    p("")
    p("[A 点] hold_days 口径显式化字段")
    st, data = get("/api/stock?symbol=600519")
    d = json.loads(data.decode("utf-8"))
    p(f"    /api/stock?symbol=600519 hold_days_basis = {d.get('hold_days_basis')!r}"
      f"  hold_days={d.get('hold_days')}")
    st, data = get("/api/positions")
    pl = json.loads(data.decode("utf-8"))
    if pl:
        who = pl[0]["player"]
        import urllib.parse
        st, data = get("/api/person?name=" + urllib.parse.quote(who))
        pd = json.loads(data.decode("utf-8"))
        modes = sorted({it.get("hold_days_mode") for it in pd.get("positions", [])})
        p(f"    /api/person?name={who} hold_days_basis = {pd.get('hold_days_basis')!r}"
          f"  hold_days_mode 取值 = {modes}")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()
    logf.close()

time.sleep(0.5)
p("")
p("=" * 78)
p(f"验收后：8799 已停止 = {not alive(PORT)}；8765 生产实例仍存活 = {alive(8765)}")
p("=" * 78)

(ROOT / "tools" / "_qa_fix_backend2.txt").write_text("\n".join(OUT) + "\n", encoding="utf-8")
print(f"\n报告已写入 {ROOT / 'tools' / '_qa_fix_backend2.txt'}")
